## Focus: Architecture

Coupling and cohesion, circular dependencies, complexity hotspots (god files, functions >50 lines), leaky abstractions, files that mix concerns, hidden dependencies between modules, patterns that make the code fragile to change.

Work structurally; do not read every file. First build the module graph from import/require statements with Grep and size files with `wc -l`. Then read only the hotspots that graph surfaces: cycles, high fan-in/fan-out modules, the largest files.
