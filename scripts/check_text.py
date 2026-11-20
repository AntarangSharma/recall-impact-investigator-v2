from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("recall", "scripts", "tests", "data", "eval", "docs", ".github", ".runtime/reports")
TEXT_SUFFIXES = {
    ".css",
    ".csv",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
ROOT_FILES = (
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    "Dockerfile",
    ".gitignore",
    ".dockerignore",
    ".python-version",
)


def main() -> None:
    paths = {ROOT / name for name in ROOT_FILES}
    paths.update(
        path
        for folder in FOLDERS
        for path in (ROOT / folder).rglob("*")
        if path.is_file() and path.suffix in TEXT_SUFFIXES
    )
    invalid: list[str] = []
    for path in sorted(paths):
        if path.is_file():
            invalid.extend(
                f"{path.relative_to(ROOT)}:{number}"
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                if chr(0x2014) in line
            )
    if invalid:
        raise SystemExit("Forbidden U+2014 at:\n" + "\n".join(invalid))
    print("First-party text and generated report punctuation checks passed.")


if __name__ == "__main__":
    main()
