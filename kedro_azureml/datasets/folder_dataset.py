import logging
from functools import partial
from operator import attrgetter
from typing import Any, Dict, Optional, Type, Union
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError
from azureml.fsspec import AzureMachineLearningFileSystem
from cachetools import Cache, cachedmethod
from cachetools.keys import hashkey
from kedro.io.core import (
    VERSION_KEY,
    VERSIONED_FLAG_KEY,
    AbstractDataSet,
    AbstractVersionedDataSet,
    DataSetError,
    DataSetNotFoundError,
    Version,
    VersionNotFoundError,
)

from kedro_azureml.client import _get_azureml_client
from kedro_azureml.datasets.pipeline_dataset import AzureMLPipelineDataSet

logger = logging.getLogger(__name__)


class AzureMLFolderDataSet(AzureMLPipelineDataSet, AbstractVersionedDataSet):
    def __init__(
        self,
        azureml_dataset: str,
        dataset: Union[str, Type[AbstractDataSet], Dict[str, Any]],
        version: Optional[Version] = None,
        folder: str = "data",
        filepath_arg: str = "filepath",
    ):
        super().__init__(dataset=dataset, folder=folder, filepath_arg=filepath_arg)

        self._azureml_dataset = azureml_dataset
        self._version = version
        # 1 entry for load version, 1 for save version
        self._version_cache = Cache(maxsize=2)  # type: Cache
        self._download = False
        self._local_run = False
        self._azureml_config = None

        # TODO: remove and disable versioning in Azure ML runner?
        if VERSION_KEY in self._dataset_config:
            raise DataSetError(
                f"'{self.__class__.__name__}' does not support versioning of the "
                f"underlying dataset. Please remove '{VERSIONED_FLAG_KEY}' flag from "
                f"the dataset definition."
            )

    @property
    def path(self) -> str:
        # For local runs we want to replicate the folder structure of the remote dataset.
        # Otherwise kedros versioning would version at the file/folder level and not the
        # AzureML dataset level
        if self._local_run:
            return (
                Path(self.folder)
                / self._azureml_dataset
                / self.resolve_load_version()
                / Path(self._dataset_config[self._filepath_arg])
            )
        else:
            return Path(self.folder) / Path(self._dataset_config[self._filepath_arg])

    @property
    def download_path(self) -> str:
        # Because `is_dir` and `is_file` don't work if the path does not
        # exist, we use this heuristic to identify paths vs folders.
        if self.path.suffix != "":
            return str(self.path.parent)
        else:
            return str(self.path)

    def _construct_dataset(self) -> AbstractDataSet:
        dataset_config = self._dataset_config.copy()
        dataset_config[self._filepath_arg] = str(self.path)
        return self._dataset_type(**dataset_config)

    def _get_latest_version(self) -> str:
        try:
            with _get_azureml_client(
                subscription_id=None, config=self._azureml_config
            ) as ml_client:
                return ml_client.data.get(self._azureml_dataset, label="latest").version
        except ResourceNotFoundError:
            raise DataSetNotFoundError(f"Did not find Azure ML Data Asset for {self}")

    @cachedmethod(cache=attrgetter("_version_cache"), key=partial(hashkey, "load"))
    def _fetch_latest_load_version(self) -> str:
        return self._get_latest_version()

    def _get_azureml_dataset(self):
        with _get_azureml_client(
            subscription_id=None, config=self._azureml_config
        ) as ml_client:
            return ml_client.data.get(
                self._azureml_dataset, version=self.resolve_load_version()
            )

    def _load(self) -> Any:
        if self._download:
            try:
                azureml_ds = self._get_azureml_dataset()
            except ResourceNotFoundError:
                raise VersionNotFoundError(
                    f"Did not find version {self.resolve_load_version()} for {self}"
                )
            fs = AzureMachineLearningFileSystem(azureml_ds.path)
            if azureml_ds.type == "uri_file":
                # relative (to storage account root) path of the file dataset on azure
                path_on_azure = fs._infer_storage_options(azureml_ds.path)[-1]
            elif azureml_ds.type == "uri_folder":
                # relative (to storage account root) path of the folder dataset on azure
                dataset_root_on_azure = fs._infer_storage_options(azureml_ds.path)[-1]
                # relative (to storage account root) path of the dataset in the folder on azure
                path_on_azure = str(
                    Path(dataset_root_on_azure)
                    / self._dataset_config[self._filepath_arg]
                )
            # we take the relative within the Azure dataset to avoid downloading
            # all files in a folder dataset.
            for fpath in fs.ls(path_on_azure):
                logger.info(f"Downloading {fpath} for local execution")
                # using APPEND will keep the local file if exists
                # as versions are unique this will prevent unnecessary file download
                fs.download(fpath, self.download_path, overwrite="APPEND")
        return self._construct_dataset().load()

    def _save(self, data: Any) -> None:
        self._construct_dataset().save(data)
