# agentive-unit: lib.agentive
# design: docs/IMPLEMENTATION.md §4 (runtime library, python3 stdlib only) [design judgment]
"""agentive run-time library, installed in user repos at .agents/lib/agentive/.

Import from a script with:  sys.path.insert(0, <repo>/.agents/lib); from agentive import ayaml, common
Modules are owned per file (docs/IMPLEMENTATION.md §7): ayaml, common = foundation (A);
add your own module file instead of editing another owner's module.
"""
