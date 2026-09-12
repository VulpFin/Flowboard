from pydantic import BaseSettings
from pathlib import Path

class Settings(BaseSettings):
    STORAGE_BACKEND: str = "vfdb"  # "json", "sqlite", etc. defaults to "vfdb".
    DATA_DIR: Path = Path(__file__).resolve().parent.parent / "data"
    VFDB_PATH: Path = DATA_DIR / "flowboard.vfdb"
    VFDB_WAL: bool = False  # if VFDB supports WAL/journal modes
    VFDB_CACHE_SIZE: int = 1024  # MB, adjust to taste

settings = Settings()
settings.DATA_DIR.mkdir(exist_ok=True, parents=True)