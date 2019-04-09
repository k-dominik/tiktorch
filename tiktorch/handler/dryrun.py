import logging
import logging.config
import torch
import traceback
import numpy
import bisect
import queue
import threading

from concurrent.futures import ThreadPoolExecutor, Future
from multiprocessing.connection import Connection
from torch import multiprocessing as mp

from torch.multiprocessing import Process, Pipe, Queue
from tiktorch import log
from inferno.extensions import criteria

from typing import (
    Any,
    List,
    Generic,
    Iterator,
    Iterable,
    Sequence,
    TypeVar,
    Mapping,
    Callable,
    Dict,
    Optional,
    Tuple,
    Union,
    NamedTuple,
)

from tiktorch.rpc import RPCInterface, exposed, Shutdown, RPCFuture
from tiktorch.rpc.mp import MPServer
from tiktorch.utils import add_logger
from tiktorch.tiktypes import (
    TikTensor,
    TikTensorBatch,
    PointAndBatchPointBase,
    PointBase,
    Point2D,
    Point3D,
    Point4D,
    BatchPointBase,
    BatchPoint2D,
    BatchPoint3D,
    BatchPoint4D,
)

from tiktorch.configkeys import (
    INPUT_CHANNELS,
    INPUT_AXIS_ORDER,
    OUTPUT_AXIS_ORDER,
    TRAINING,
    BATCH_SIZE,
    TRAINING_SHAPE,
    TRAINING_SHAPE_LOWER_BOUND,
    TRAINING_SHAPE_UPPER_BOUND,
    NUM_ITERATION_DONE,
    MAX_NUM_ITERATIONS_PER_UPDATE,
    LOSS_CRITERION_CONFIG,
    OPTIMIZER_CONFIG,
)


def ret_through_conn(*args, fn: Callable, send_conn: Connection, **kwargs) -> None:
    send_conn.send(fn(*args, **kwargs))


def in_subproc(fn: Callable, *args, **kwargs) -> Connection:
    """
    Run 'fn' in a subprocess and return a connection that will hold the result
    :param fn: function to run in subprocess
    :return: Connection to result
    """
    recv_conn, send_conn = Pipe(duplex=False)
    subproc = Process(
        target=ret_through_conn, name=fn.__name__, args=args, kwargs={"fn": fn, "send_conn": send_conn, **kwargs}
    )
    subproc.start()
    return recv_conn


class IDryRun(RPCInterface):
    @exposed
    def dry_run(
        self,
        devices: List[torch.device],
        training_shape: Optional[Union[Point2D, Point3D, Point4D]] = None,
        valid_shapes: Optional[List[Union[Point2D, Point3D, Point4D]]] = None,
        shrinkage: Optional[Union[Point2D, Point3D, Point4D]] = None,
    ) -> RPCFuture:
        raise NotImplementedError

    @exposed
    def update_config(self, partial_config: dict) -> None:
        raise NotImplementedError

    @exposed
    def shutdown(self) -> Shutdown:
        raise NotImplementedError


def run(conn: Connection, config: dict, model: torch.nn.Module, log_queue: Optional[mp.Queue] = None):
    log.configure(log_queue)
    # print('CUDA_VISIBLE_DEVICES:', os.environ["CUDA_VISIBLE_DEVICES"])
    dryrun_proc = DryRunProcess(config, model)
    srv = MPServer(dryrun_proc, conn)
    srv.listen()


class DryRunProcess(IDryRun):
    """
    Process to execute a dry run to determine training and inference shape for 'model' on 'device'
    """

    def __init__(self, config: dict, model: torch.nn.Module) -> None:
        self.logger = logging.getLogger(__name__)
        self.logger.info("started")
        self.config = config
        self.model = model

        method = config[TRAINING][LOSS_CRITERION_CONFIG]["method"]
        # get loss criterion like Inferno does for isinstance(method, str):
        # Look for criteria in torch
        criterion_class = getattr(torch.nn, method, None)
        if criterion_class is None:
            # Look for it in inferno extensions
            criterion_class = getattr(criteria, method, None)
        if criterion_class is None:
            raise ValueError(f"Criterion {method} not found.")

        self.criterion_class = criterion_class

        self.shutdown_event = threading.Event()

        self.training_shape = None
        self.valid_shapes = None
        self.shrinkage: Optional[Union[Point2D, Point3D, Point4D]] = None

        self.dry_run_queue = queue.Queue()
        self.dry_run_thread = threading.Thread(target=add_logger(self.logger)(self._dry_run_worker), name="DryRun")
        self.dry_run_thread.start()

    def update_config(self, partial_config: dict) -> None:
        for key, value in partial_config.items():
            if isinstance(partial_config[key], dict):
                for subkey, subvalue in partial_config[key].items():
                    if subvalue is None:
                        if subkey in self.config[key]:
                            del self.config[key][subkey]
                    else:
                        self.config[key][subkey] = subvalue
            elif value is None:
                if key in self.config:
                    del self.config[key]
            else:
                self.config[key] = value

    def _dry_run_worker(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                args = self.dry_run_queue.get(block=True, timeout=1)
            except queue.Empty:
                pass
            else:
                self._dry_run(*args)

    def dry_run(
        self,
        devices: List[torch.device],
        training_shape: Optional[Union[Point2D, Point3D, Point4D]] = None,
        valid_shapes: Optional[List[Union[Point2D, Point3D, Point4D]]] = None,
        shrinkage: Optional[Union[Point2D, Point3D, Point4D]] = None,
    ) -> RPCFuture[
        Union[
            Tuple[List[torch.device], Point2D, List[Point2D], Point2D],
            Tuple[List[torch.device], Point3D, List[Point3D], Point3D],
            Tuple[List[torch.device], Point4D, List[Point4D], Point4D],
        ]
    ]:
        fut = RPCFuture()
        self.dry_run_queue.put((devices, training_shape, valid_shapes, shrinkage, fut))
        return fut

    def _dry_run(
        self,
        devices: List[torch.device],
        training_shape: Optional[Union[Point2D, Point3D, Point4D]],
        valid_shapes: Optional[List[Union[Point2D, Point3D, Point4D]]],
        shrinkage: Optional[Union[Point2D, Point3D, Point4D]],
        fut: Future,
    ) -> None:
        self.logger.info("Starting dry run for %s", devices)
        try:
            if not devices:
                raise ValueError(f"Dry run on empty device list")

            if self.shrinkage is None:
                self.shrinkage = shrinkage
            elif shrinkage is not None and shrinkage != self.shrinkage:
                raise ValueError(f"given shrinkage {shrinkage} incompatible with self.shrinkage {self.shrinkage}")

            working_devices = self.minimal_device_test(devices)
            failed_devices = set(devices) - set(working_devices)
            if failed_devices:
                self.logger.error(f"Minimal device test failed for {failed_devices}")

            if self.training_shape is None:
                self.training_shape = self._determine_training_shape(training_shape=training_shape, devices=devices)
            elif training_shape is not None and self.training_shape != training_shape:
                raise ValueError(
                    f"given training_shape {training_shape} incompatible with self.training_shape {self.training_shape}"
                )

            self._determine_valid_shapes(devices=devices, valid_shapes=valid_shapes)

            fut.set_result((devices, self.training_shape, self.valid_shapes, self.shrinkage))
            self.logger.info("dry run done")
        except Exception as e:
            self.logger.error(traceback.format_exc())
            fut.set_exception(e)

    def _determine_training_shape(
        self, devices: Sequence[torch.device], training_shape: Optional[Union[Point2D, Point3D, Point4D]] = None
    ):
        self.logger.debug("Determine training shape on %s (previous training shape: %s)", devices, training_shape)
        batch_size = self.config[TRAINING][BATCH_SIZE]
        input_channels = self.config[INPUT_CHANNELS]

        if TRAINING_SHAPE in self.config[TRAINING]:
            # validate given training shape
            config_training_shape = PointBase.from_spacetime(input_channels, self.config[TRAINING][TRAINING_SHAPE])
            if training_shape is None:
                training_shape = config_training_shape
            else:
                assert training_shape == config_training_shape, "training shape unequal to config training shape"

            training_shape = training_shape.add_batch(batch_size)

            if TRAINING_SHAPE_UPPER_BOUND in self.config[TRAINING]:
                training_shape_upper_bound = BatchPointBase.from_spacetime(
                    batch_size, input_channels, self.config[TRAINING][TRAINING_SHAPE_UPPER_BOUND]
                )
                if not (training_shape <= training_shape_upper_bound):
                    raise ValueError(
                        f"{TRAINING_SHAPE}: {training_shape} incompatible with {TRAINING_SHAPE_UPPER_BOUND}: {training_shape_upper_bound}"
                    )

            if TRAINING_SHAPE_LOWER_BOUND in self.config[TRAINING]:
                training_shape_lower_bound = BatchPointBase.from_spacetime(
                    batch_size, input_channels, self.config[TRAINING][TRAINING_SHAPE_LOWER_BOUND]
                )
            else:
                training_shape_lower_bound = training_shape.__class__()

            if not (training_shape_lower_bound <= training_shape):
                raise ValueError(
                    f"{TRAINING_SHAPE_LOWER_BOUND}{training_shape_lower_bound} incompatible with {TRAINING_SHAPE}{training_shape}"
                )

            if not self.validate_shape(devices=devices, shape=training_shape, train_mode=True):
                raise ValueError(f"{TRAINING_SHAPE}: {training_shape} could not be processed on devices: {devices}")
        else:
            # determine a valid training shape
            if TRAINING_SHAPE_UPPER_BOUND not in self.config[TRAINING]:
                raise ValueError(f"config is missing {TRAINING_SHAPE} and/or {TRAINING_SHAPE_UPPER_BOUND}.")

            training_shape_upper_bound = BatchPointBase.from_spacetime(
                batch_size, input_channels, self.config[TRAINING][TRAINING_SHAPE_UPPER_BOUND]
            )

            if TRAINING_SHAPE_LOWER_BOUND in self.config[TRAINING]:
                training_shape_lower_bound = BatchPointBase.from_spacetime(
                    batch_size, input_channels, self.config[TRAINING][TRAINING_SHAPE_LOWER_BOUND]
                )
            else:
                training_shape_lower_bound = training_shape_upper_bound.__class__()

            if not (training_shape_lower_bound <= training_shape_upper_bound):
                raise ValueError(
                    f"{TRAINING_SHAPE_LOWER_BOUND}: {training_shape_lower_bound} incompatible with "
                    f"{TRAINING_SHAPE_UPPER_BOUND}: {training_shape_upper_bound}"
                )

            # find optimal training shape
            training_shape = self.find_one_shape(
                training_shape_lower_bound, training_shape_upper_bound, devices=devices
            )

        return training_shape.drop_batch()

    def _determine_valid_shapes(
        self, devices: Sequence[torch.device], valid_shapes: Sequence[Union[Point2D, Point3D, Point4D]]
    ):
        # todo: find valid shapes
        if valid_shapes is None:
            self.valid_shapes = [self.training_shape]
        else:
            self.valid_shapes = [
                self.validate_shape(devices=devices, shape=s.add_batch(1), train_mode=False) for s in valid_shapes
            ]

    def minimal_device_test(self, devices: Sequence[torch.device]) -> Sequence[torch.device]:
        conns = [in_subproc(self._minimal_device_test, device=d) for d in devices]
        return [d for d, c in zip(devices, conns) if c.recv()]

    @staticmethod
    def _minimal_device_test(device: torch.device) -> bool:
        """
        Minimalistic test to check if a toy model can be loaded onto the device
        :return: True on success, False otherwise
        """
        try:
            with torch.no_grad():
                model = torch.nn.Conv2d(1, 1, 1).to(device)
                x = torch.zeros(1, 1, 1, 1).to(device)
                y = model(x)
                del model, x, y
        except Exception as e:
            return False

        return True

    def validate_shape(
        self, devices: Sequence[torch.device], shape: Union[BatchPoint2D, BatchPoint3D, BatchPoint4D], train_mode: bool
    ) -> bool:
        assert devices
        if train_mode:
            crit_class = self.criterion_class
            criterion_kwargs = {
                key: value for key, value in self.config[TRAINING][LOSS_CRITERION_CONFIG].items() if key != "method"
            }
        else:
            crit_class = None
            criterion_kwargs = {}

        return_conns = [
            in_subproc(
                self._validate_shape,
                model=self.model,
                device=d,
                shape=[shape[a] for a in self.config[INPUT_AXIS_ORDER]],
                criterion_class=crit_class,
                criterion_kwargs=criterion_kwargs,
            )
            for d in devices
        ]
        output_shapes = [conn.recv() for conn in return_conns]
        for e in output_shapes:
            if isinstance(e, Exception):
                self.logger.debug("Shape %s invalid: %r", shape, e)
                return False

        out = output_shapes[0]
        if any([o != out for o in output_shapes[1:]]):
            self.logger.warning("different devices returned different output shapes for same input shape!")
            return False

        output_axis_order = self.config[OUTPUT_AXIS_ORDER]
        if "t" in output_axis_order:
            output_shape = Point4D
        elif "z" in output_axis_order:
            output_shape = Point3D
        else:
            output_shape = Point2D

        output_shape = output_shape(**{a: s for a, s in zip(output_axis_order, out) if a != "b"})

        shrinkage = shape.drop_batch() - output_shape

        if self.shrinkage is None:
            self.shrinkage = shrinkage
            self.logger.info("Determined shrinkage to be {%s}", shrinkage)
            return True
        else:
            return self.shrinkage == shrinkage

    @staticmethod
    def _validate_shape(
        model: torch.nn.Module,
        device: torch.device,
        shape: List[int],
        criterion_class: Optional[type],
        criterion_kwargs: Dict,
    ) -> Optional[Tuple[Union[Point2D, Point3D, Point4D], int]]:
        try:

            def apply_model():
                input = torch.rand(*shape)

                if criterion_class is None:
                    with torch.no_grad():
                        output = model.to(device)(input)
                else:
                    output = model.to(device)(input)
                    target = torch.randn_like(output)
                    criterion_class(**criterion_kwargs).to(device)(output, target).backward()

                return output

            if device.type == "cpu":
                output = apply_model()
            else:
                with device:
                    output = apply_model()

        except Exception as e:
            return e

        return output.shape

    def find_one_shape(
        self,
        lower_limit: Union[BatchPoint2D, BatchPoint3D, BatchPoint4D],
        upper_limit: Union[BatchPoint2D, BatchPoint3D, BatchPoint4D],
        devices: Sequence[torch.device],
        train_mode: bool = False,
        discard: float = 0,
    ) -> Optional[Union[BatchPoint2D, BatchPoint3D, BatchPoint4D]]:
        shape_class = type(lower_limit)
        assert (
            type(upper_limit) == shape_class
        ), f"type(upper_limit){type(upper_limit)} == type(lower_limit){type(lower_limit)}"
        lower_limit = numpy.array(lower_limit)
        upper_limit = numpy.array(upper_limit)
        diff = upper_limit - lower_limit
        assert all(diff >= 0), f"negative diff: {diff} = upper_limit({upper_limit}) - lower_limit({lower_limit}) "
        assert 0 <= discard < 1

        def update_nonzero(diff):
            nonzero_index = diff.nonzero()[0]
            nonzero = diff[nonzero_index]
            ndiff = len(nonzero)
            return nonzero_index, nonzero, ndiff

        nonzero_index, nonzero, ndiff = update_nonzero(diff)

        ncomb = numpy.prod(nonzero)
        if ncomb > 10000:
            self.logger.warning("Possibly testing too many combinations!!!")

        while ndiff:
            search_order = numpy.argsort(nonzero)[::-1]
            for diff_i in search_order:
                shape = shape_class(*(lower_limit + diff))
                if self.validate_shape(devices=devices, shape=shape, train_mode=train_mode):
                    return shape

                reduced = int((1.0 - discard) * nonzero[diff_i] - 1)
                diff[nonzero_index[diff_i]] = reduced

            nonzero_index, nonzero, ndiff = update_nonzero(diff)

        return None

    def shutdown(self) -> Shutdown:
        self.logger.debug("Shutting down...")
        self.shutdown_event.set()
        try:
            self.dry_run_thread.join(timeout=20)
        except TimeoutError as e:
            self.logger.error(e)

        self.logger.debug("Shutdown complete")
        return Shutdown()
