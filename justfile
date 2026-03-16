default:
    just --list

clean:
    #!/usr/bin/env bash
    rm -rf build

build:
    #!/usr/bin/env bash
    gomplate

watch:
    #!/usr/bin/env bash
    watchexec -i "build/**" -- gomplate
