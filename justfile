# Address of the live map data feed. Override with `just map_url=... fetch`,
# or set FARP_MAP_URL in your shell.
map_url := env_var_or_default("FARP_MAP_URL", "")

# Show the available commands
default:
    just --list

# Install the Python packages the scripts need
install:
    #!/usr/bin/env bash
    set -euo pipefail

    # `python -m pip` rather than `pip`, so the packages always land in the
    # interpreter that will run the scripts rather than whichever pip happens to
    # be first on PATH. uv-created virtualenvs have no pip at all, hence the
    # fallback.
    if python -m pip --version >/dev/null 2>&1; then
        python -m pip install -r scripts/requirements.txt
    elif command -v uv >/dev/null 2>&1; then
        uv pip install -r scripts/requirements.txt
    else
        echo "Neither pip nor uv is available to $(command -v python)" >&2
        exit 1
    fi

# Download live map data to build/map.json
fetch-map:
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/fetch-map-data.sh --force --verbose {{ quote(map_url) }}

# Rewrite the FARP entries in locations.toml from live map data
update-data: fetch-map
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/update_locations.py --verbose

# Show what live map data would change, without writing anything
preview-data: fetch-map
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/update_locations.py --dry-run --verbose

# Run the generator against the test fixture, without writing anything
test:
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/update_locations.py --map-data tests/fixtures/map.json --dry-run --verbose

# Check the location data for anything missing or inconsistent
lint:
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/lint_locations.py

# Build the kneeboard pages into build/
build:
    #!/usr/bin/env bash
    set -euo pipefail
    gomplate

# Rebuild the pages whenever a file changes
watch:
    #!/usr/bin/env bash
    set -euo pipefail
    watchexec -i "build/**" -- gomplate

# Fetch, update the data, check it, and rebuild the pages
all: update-data lint build

# Render the pages to the PDF and PNGs that the release publishes
export: build
    #!/usr/bin/env bash
    set -euo pipefail

    # Chrome is not on PATH on macOS, so look in the usual place before giving up.
    chrome="${CHROME:-}"
    if [[ -z "${chrome}" ]]; then
        for candidate in \
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
            "google-chrome" \
            "chromium"; do
            if [[ -x "${candidate}" ]] || command -v "${candidate}" >/dev/null 2>&1; then
                chrome="${candidate}"
                break
            fi
        done
    fi
    if [[ -z "${chrome}" ]]; then
        echo "Could not find Chrome. Set CHROME to its path and try again." >&2
        exit 1
    fi
    if ! command -v pdftoppm >/dev/null 2>&1; then
        echo "pdftoppm is missing. Install poppler (brew install poppler)." >&2
        exit 1
    fi

    mkdir -p build/png
    # --virtual-time-budget: wait for the webfont before printing
    "${chrome}" \
        --headless=new \
        --disable-gpu \
        --virtual-time-budget=10000 \
        --no-pdf-header-footer \
        --print-to-pdf=build/rotorheads_comms-and-nav-reference.pdf \
        build/comms-nav.html
    pdftoppm -png \
        build/rotorheads_comms-and-nav-reference.pdf \
        build/png/rotorheads_comms-and-nav-reference
    echo "Wrote build/rotorheads_comms-and-nav-reference.pdf and build/png/"

# Preview the release notes for everything since the last release
release-notes:
    #!/usr/bin/env bash
    set -euo pipefail

    commits="$(mktemp)"
    previous_data="$(mktemp)"
    trap 'rm -f "${commits}" "${previous_data}"' EXIT

    previous="$(git tag --list 'v*' --sort=-v:refname | head -n 1)"
    args=(--current data/locations.toml --commits "${commits}")
    if [[ -n "${previous}" ]]; then
        git log --no-merges --pretty=format:'%s' "${previous}..HEAD" > "${commits}"
        if git show "${previous}:data/locations.toml" > "${previous_data}" 2>/dev/null; then
            args+=(--previous "${previous_data}" --previous-tag "${previous}")
        fi
    fi
    ./scripts/release_notes.py "${args[@]}"

# Delete the build output
clean:
    #!/usr/bin/env bash
    set -euo pipefail
    rm -rf build
