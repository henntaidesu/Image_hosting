"""Picture Bed entry point.

The implementation lives in ``src``. This remains the only startup entry used
by Python, the Windows batch files, and PyInstaller.
"""

from src import config, database, security, storage
from src.app_factory import create_app
from src.server import run_server as run_waitress


app = create_app()

# Compatibility exports for integrations and the existing test suite.
BASE_DIR = config.PROJECT_DIR
RUNTIME_DIR = config.RUNTIME_DIR
DATA_DIR = config.DATA_DIR
UPLOAD_DIR = config.UPLOAD_DIR
DATABASE = config.DATABASE
MAX_UPLOAD_FILES = config.MAX_UPLOAD_FILES
StorageUnavailableError = storage.StorageUnavailableError
shutil = storage.shutil


def _sync_runtime_paths():
    config.RUNTIME_DIR = RUNTIME_DIR
    config.DATA_DIR = DATA_DIR
    config.UPLOAD_DIR = UPLOAD_DIR
    config.DATABASE = DATABASE


def init_storage():
    _sync_runtime_paths()
    return database.init_storage()


def get_setting(key):
    _sync_runtime_paths()
    return database.get_setting(key)


def set_setting(key, value):
    _sync_runtime_paths()
    return database.set_setting(key, value)


def get_db():
    _sync_runtime_paths()
    return database.get_db()


def configure_runtime_security(max_upload_mb=None, public_base_url=None):
    _sync_runtime_paths()
    return security.configure_runtime_security(app, max_upload_mb, public_base_url)


def run_server():
    run_waitress(app)


if __name__ == "__main__":
    run_server()
