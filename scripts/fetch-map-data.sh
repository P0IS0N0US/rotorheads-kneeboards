#!/usr/bin/env bash
#
# fetch-map-data.sh - Download live map data to a known location.
#
# Nothing else in the repo talks to the server. Everything downstream reads the
# file this writes, so a run can always be repeated on the same input.
#
# Usage: fetch-map-data.sh [OPTIONS] <url>
#

set -euo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

readonly DEFAULT_OUTPUT="build/map.json"

MAP_URL="${FARP_MAP_URL:-}"
OUTPUT="${DEFAULT_OUTPUT}"
FORCE=0
TIMEOUT=30
DRY_RUN=0
QUIET=0
VERBOSE=0

DOWNLOAD_FILE=""

log_error() {
    printf '[%s] %-8s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "ERROR:" "$*" >&2
}

log_warning() {
    [[ ${QUIET} -eq 1 ]] && return 0
    printf '[%s] %-8s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "WARNING:" "$*"
}

log_info() {
    [[ ${QUIET} -eq 1 ]] && return 0
    printf '[%s] %-8s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "INFO:" "$*"
}

log_debug() {
    [[ ${QUIET} -eq 1 ]] && return 0
    [[ ${VERBOSE} -eq 0 ]] && return 0
    printf '[%s] %-8s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "DEBUG:" "$*"
}

cleanup() {
    [[ -n "${DOWNLOAD_FILE}" && -f "${DOWNLOAD_FILE}" ]] && rm -f "${DOWNLOAD_FILE}"
    return 0
}
trap cleanup EXIT

usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [OPTIONS] <url>

Download live map data for the other scripts to read.

Arguments:
  url                   Address of the map data feed. May also be supplied
                        through FARP_MAP_URL.

Options:
  -o, --output <file>   Where to write it (default: ${DEFAULT_OUTPUT})
  -f, --force           Replace the output file if it already exists
      --timeout <secs>  Give up after this long (default: ${TIMEOUT})
      --dry-run         Show what would happen without downloading
  -q, --quiet           Suppress all output except errors
  -v, --verbose         Enable verbose output
  -h, --help            Display this help message

Examples:
  # Normal use
  ${SCRIPT_NAME} --force http://example.invalid/mapdata/map.json

  # Grab a capture to work from, without touching the usual output
  ${SCRIPT_NAME} -o captures/latest.json http://example.invalid/mapdata/map.json

  # Keep a dated copy
  ${SCRIPT_NAME} -o "captures/map-\$(date -u +%Y%m%d).json" http://example.invalid/mapdata/map.json

Environment:
  FARP_MAP_URL          Used when no url argument is given
EOF
}

parse_arguments() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            -o|--output)
                OUTPUT="$2"
                shift 2
                ;;
            -f|--force)
                FORCE=1
                shift
                ;;
            --timeout)
                TIMEOUT="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            -q|--quiet)
                QUIET=1
                shift
                ;;
            -v|--verbose)
                VERBOSE=1
                QUIET=0
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            -*)
                log_error "Unknown option: $1"
                usage
                exit 2
                ;;
            *)
                MAP_URL="$1"
                shift
                ;;
        esac
    done
}

# A feed that returns an error page, or truncates mid-download, is worse than one
# that fails outright: it reads as every FARP having been decommissioned. Check
# the shape before anything is allowed to replace the previous copy.
validate_download() {
    local path="$1"

    python3 - "${path}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        document = json.load(handle)
except json.JSONDecodeError as error:
    raise SystemExit(f"not valid JSON ({error})")

features = document.get("features")
if not isinstance(features, list) or not features:
    raise SystemExit("no features in the downloaded data")
print(len(features))
PY
}

main() {
    parse_arguments "$@"

    if [[ -z "${MAP_URL}" ]]; then
        log_error "No map data url given. Pass one as an argument or set FARP_MAP_URL."
        usage
        exit 2
    fi

    if ! command -v curl >/dev/null 2>&1; then
        log_error "Required command not found: curl"
        exit 127
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        log_error "Required command not found: python3"
        exit 127
    fi

    cd "${REPO_ROOT}"

    # Refusing by default is what stops a mistyped --output from quietly
    # replacing the test fixture, or anything else already in the repo.
    if [[ -e "${OUTPUT}" && ${FORCE} -eq 0 ]]; then
        log_error "${OUTPUT} already exists. Pass --force to replace it."
        exit 1
    fi

    if [[ ${DRY_RUN} -eq 1 ]]; then
        log_info "[DRY RUN] Would download ${MAP_URL} to ${OUTPUT}"
        return 0
    fi

    # --fail: treat an HTTP error as a failure instead of saving the error page
    # --location: follow redirects
    # --write-out: the body goes to the file, so stdout carries only these stats
    local curl_args=(--fail --location --silent --show-error --max-time "${TIMEOUT}"
                     --write-out '%{http_code} %{size_download}')

    DOWNLOAD_FILE="$(mktemp)"
    log_info "Downloading ${MAP_URL}"
    local stats
    if ! stats="$(curl "${curl_args[@]}" -o "${DOWNLOAD_FILE}" "${MAP_URL}")"; then
        log_error "Could not download ${MAP_URL}"
        exit 1
    fi
    log_debug "HTTP ${stats%% *}, ${stats##* } bytes"

    local feature_count
    if ! feature_count="$(validate_download "${DOWNLOAD_FILE}" 2>&1)"; then
        log_error "Downloaded data is not usable map data: ${feature_count}"
        exit 1
    fi

    mkdir -p "$(dirname "${OUTPUT}")"
    mv "${DOWNLOAD_FILE}" "${OUTPUT}"
    # mktemp creates 0600; the result is not sensitive and may get committed.
    chmod 644 "${OUTPUT}"
    DOWNLOAD_FILE=""
    log_info "Wrote ${feature_count} features to ${OUTPUT}"

    if git ls-files --error-unmatch "${OUTPUT}" >/dev/null 2>&1; then
        log_warning "${OUTPUT} is tracked by git, so this will show up as a change."
        log_warning "The test fixture is hand-built, not a capture, so think twice"
        log_warning "before replacing one with the other."
    fi
}

main "$@"
