import logging
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence
from zipfile import ZipFile

from pybio import spec
from pybio.spec.utils import train

from tiktorch.server.model_adapter import ModelAdapter, create_model_adapter

MODEL_EXTENSIONS = (".model.yaml", ".model.yml")
logger = logging.getLogger(__name__)


def guess_model_path(file_names: List[str]) -> Optional[str]:
    for file_name in file_names:
        if file_name.endswith(MODEL_EXTENSIONS):
            return file_name

    return None


def eval_model_zip(model_zip: ZipFile, devices: Sequence[str], cache_path: Optional[Path] = None) -> ModelAdapter:
    temp_path = Path(tempfile.mkdtemp(prefix="tiktorch_"))
    if cache_path is None:
        cache_path = temp_path / "cache"

    model_zip.extractall(temp_path)

    spec_file_str = guess_model_path([str(file_name) for file_name in temp_path.glob("*")])
    if not spec_file_str:
        raise Exception(
            "Model config file not found, make sure that .model.yaml file in the root of your model archive"
        )

    pybio_model = spec.utils.load_model(spec_file_str, root_path=temp_path, cache_path=cache_path)

    if pybio_model.spec.training is None:
        return create_model_adapter(pybio_model=pybio_model, devices=devices)
    else:
        ret = train(pybio_model, _devices=devices)

    def _on_error(function, path, exc_info):
        logger.warning("Failed to delete temp directory %s", path)

    shutil.rmtree(temp_path, onerror=_on_error)

    return ret
