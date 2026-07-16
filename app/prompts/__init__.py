from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=64)
def load_prompt(name: str) -> str:
    return files(__package__).joinpath(name).read_text(encoding="utf-8").strip()
