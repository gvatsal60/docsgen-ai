TOP_DIR := `git rev-parse --show-toplevel`
SRC_DIR := TOP_DIR / "src"
TESTS_DIR := TOP_DIR / "tests"
COV_DIR := TOP_DIR / "coverage"

[private]
default:
    @just --list --justfile {{justfile()}}

# Install or update project dependencies with `uv`
sync:
    @uv sync --no-cache --quiet

# Run the Streamlit application development server
dev: sync
    @uv run streamlit run app.py \
    --server.runOnSave true \
    --server.headless=false \
    --server.address=0.0.0.0

# Run the main application entrypoint
run: sync
    @uv run streamlit run app.py
    --server.headless=true \
    --server.address=0.0.0.0

# Run the test target
test: sync
    @uv run --group dev pytest {{ TESTS_DIR }} -v --no-cov

# Run tests with coverage report
coverage: sync
    @uv run --group dev pytest {{ TESTS_DIR }} -v \
    --cov-report=html:{{ COV_DIR }} \
    --cov-report=xml:{{ COV_DIR }}/coverage.xml \
    --cov-report=lcov:{{ COV_DIR }}/lcov.info

# Run tests and generate both coverage and test results for Codecov
codecov: sync
    @uv run --group dev pytest --cov --cov-branch --cov-report=xml:{{ COV_DIR }}/coverage.xml --junitxml={{ COV_DIR }}/junit.xml -o junit_family=legacy

# Remove the local environment and generated Python caches
clean:
    @uv clean --quiet --force
    @rm -rf __pycache__ .pytest_cache .mypy_cache .venv {{ COV_DIR }}
    @find . -type d \( -name "__pycache__" -o -name ".pytest_cache" -o -name ".mypy_cache" \) -prune -exec rm -rf {} +