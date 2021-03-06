import logging
from typing import Any, List, Sequence

import torch
from pybio.core.transformations.base import make_concatenated_apply
from pybio.spec import nodes
from pybio.spec.utils import get_instance

from ._base import ModelAdapter
from ._utils import has_batch_dim

logger = logging.getLogger(__name__)


def _noop(tensor):
    return tensor


def _remove_batch_dim(batch: List):
    return [t.reshape(t.shape[1:]) for t in batch]


def _add_batch_dim(tensor):
    return tensor.reshape((1,) + tensor.shape)


class Exemplum(ModelAdapter):
    def __init__(
        self,
        *,
        pybio_model: nodes.Model,
        devices=Sequence[str],
    ):
        self._max_num_iterations = 0
        self._iteration_count = 0
        spec = pybio_model.spec
        self.name = spec.name

        if len(spec.inputs) != 1 or len(spec.outputs) != 1:
            raise NotImplementedError("Only single input, single output models are supported")

        assert len(spec.inputs) == 1
        assert len(spec.outputs) == 1
        _input = spec.inputs[0]
        _output = spec.outputs[0]

        self._internal_input_axes = _input.axes
        self._internal_output_axes = _output.axes

        if has_batch_dim(self._internal_input_axes):
            self.input_axes = self._internal_input_axes[1:]
            self._input_batch_dimension_transform = _add_batch_dim
            _input_shape = _input.shape[1:]
        else:
            self.input_axes = self._internal_input_axes
            self._input_batch_dimension_transform = _noop
            _input_shape = _input.shape

        self.input_shape = list(zip(self.input_axes, _input_shape))

        _halo = _output.halo or [0 for _ in _output.axes]

        if has_batch_dim(self._internal_output_axes):
            self.output_axes = self._internal_output_axes[1:]
            self._output_batch_dimension_transform = _remove_batch_dim
            _halo = _halo[1:]
        else:
            self.output_axes = self._internal_output_axes
            self._output_batch_dimension_transform = _noop

        self.halo = list(zip(self.output_axes, _halo))

        self.model = get_instance(pybio_model)
        if spec.framework == "pytorch":
            self.devices = [torch.device(d) for d in devices]
            self.model.to(self.devices[0])
            assert isinstance(self.model, torch.nn.Module)
            if spec.prediction.weights is not None:
                state = torch.load(spec.prediction.weights.source, map_location=self.devices[0])
                self.model.load_state_dict(state)
        # elif spec.framework == "tensorflow":
        #     import tensorflow as tf
        #     self.devices = []
        #     tf_model = tf.keras.models.load_model(spec.prediction.weights.source)
        #     self.model.set_model(tf_model)
        else:
            raise NotImplementedError

        self._prediction_preprocess = make_concatenated_apply([get_instance(tf) for tf in spec.prediction.preprocess])
        self._prediction_postprocess = make_concatenated_apply([get_instance(tf) for tf in spec.prediction.postprocess])

    @property
    def max_num_iterations(self) -> int:
        return self._max_num_iterations

    @property
    def iteration_count(self) -> int:
        return self._iteration_count

    def forward(self, batch) -> List[Any]:
        batch = torch.from_numpy(batch)
        with torch.no_grad():
            batch = self._input_batch_dimension_transform(batch)
            batch = self._prediction_preprocess(batch)
            batch = [b.to(self.devices[0]) for b in batch]
            batch = self.model(*batch)
            batch = self._prediction_postprocess(batch)
            batch = self._output_batch_dimension_transform(batch)
            assert all([bs > 0 for bs in batch[0].shape]), batch[0].shape
            result = batch[0]
            if isinstance(result, torch.Tensor):
                return result.detach().cpu().numpy()
            else:
                return result

    def set_max_num_iterations(self, max_num_iterations: int) -> None:
        self._max_num_iterations = max_num_iterations

    def set_break_callback(self, cb):
        return NotImplementedError

    def fit(self):
        raise NotImplementedError

    def train(self):
        raise NotImplementedError
