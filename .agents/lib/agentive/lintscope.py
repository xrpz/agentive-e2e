# agentive-unit: lib.agentive
# design: spec.enforcement.hooks.post_tool_use, spec.init_apply.installed_layer_excluded, contracts[manifest] · owner B
"""Which edited files the PostToolUse lint hook lints (spec.enforcement.hooks.post_tool_use.scope).

- lint_extensions(manifest): the extensions the lint verb covers. `verbs.lint.exts` (list such as [".ts", ".tsx"])
  when the manifest sets it; otherwise derived from the ecosystem the manifest's own evidence files name
  (go.mod -> .go, package.json -> .js .ts ..., pyproject.toml -> .py ...). Documentation and data extensions
  (.md, .txt, .yaml ...) are never derived.
- paths_from_hook_input(inp): the edited paths of one PostToolUse input. Claude Code: tool_input.file_path (and
  edits[].file_path). Codex apply_patch has no file_path: the paths are parsed from the patch text in
  tool_input.command (or .input / .patch) from its `*** Add File: / Update File: / Delete File: / Move to:`
  headers. Returns (paths, ok); ok is False when an apply_patch text yielded no path (the hook then skips and Stop
  covers the edit). The header format is taken from the Codex apply_patch tool instructions (unverified against the
  official page in this repo).
- select_files(root, paths, exts): never a file of the installed agentive layer (.agents, .claude, .codex): the
  project's linter does not own it (E2E-01)
- file_command(cmd, files, targets): the per-file form of the lint command (E2E-04). A trailing whole-repo target
  (`.`, `./`, `./...`, or one of the agentive manifest's src/tests paths) is REPLACED by the files, so
  `uv run ruff check --extend-exclude .agents,.claude,.codex .` becomes `... --extend-exclude .agents,.claude,.codex
  src/a.py` instead of `... . src/a.py` (which lints the whole repository again). A command with no such target gets
  the files appended; a command joined by a shell operator, or extra arguments that are flags (`verb lint -- --fix`),
  leave the command whole and are appended [design judgment].
"""
import os
import re
import shlex

__all__ = ["lint_extensions", "paths_from_hook_input", "parse_patch_paths", "select_files", "file_command", "DOC_EXTS",
           "OWNED_DIRS"]

OWNED_DIRS = (".agents", ".claude", ".codex")   # the installed layer; same list as the engine's evidence.OWNED_DIRS
_REPO_TARGETS = (".", "./", "./...")
_SHELL_JOIN = re.compile(r"&&|\|\||;|\||\$\(|`|>|<")

DOC_EXTS = {".md", ".mdx", ".markdown", ".txt", ".rst", ".adoc", ".json", ".yaml", ".yml", ".toml", ".lock", ".csv", ".svg"}

# evidence file name (or suffix) -> extensions of that ecosystem's source files [design judgment: the map is small and
# conservative; a stack it does not know gets no PostToolUse lint, Stop still runs the whole lint verb]
_ECOSYSTEMS = (
    (("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock", "tsconfig.json", "deno.json"),
     (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")),
    (("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile", "poetry.lock", "uv.lock", "tox.ini"), (".py", ".pyi")),
    (("go.mod", "go.sum"), (".go",)),
    (("Cargo.toml", "Cargo.lock"), (".rs",)),
    (("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradle.lockfile"), (".java", ".kt", ".kts")),
    (("Gemfile", "Gemfile.lock"), (".rb",)),
    (("composer.json", "composer.lock"), (".php",)),
    (("mix.exs", "mix.lock"), (".ex", ".exs")),
    (("Package.swift",), (".swift",)),
)

_HEADER = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.M)


def lint_extensions(manifest):
    lint = ((manifest or {}).get("verbs") or {}).get("lint")
    if not isinstance(lint, dict):
        return set()
    exts = lint.get("exts")
    if isinstance(exts, list) and exts:
        return {(e if str(e).startswith(".") else "." + str(e)).lower() for e in exts}
    names = set()
    for v in ((manifest or {}).get("verbs") or {}).values():
        if isinstance(v, dict):
            names.update(os.path.basename(f) for f in ((v.get("evidence") or {}).get("files") or []) if isinstance(f, str))
    out = set()
    for files, es in _ECOSYSTEMS:
        if any(n in names for n in files):
            out.update(es)
    for n in names:                                  # *.csproj / *.sln style evidence
        if n.endswith((".csproj", ".sln")):
            out.add(".cs")
    return out - DOC_EXTS


def parse_patch_paths(text):
    """Paths named by apply_patch headers, in order, without duplicates. [] when none."""
    seen, out = set(), []
    for m in _HEADER.finditer(text or ""):
        p = m.group(1).strip().strip("\"'")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def paths_from_hook_input(inp):
    ti = inp.get("tool_input") if isinstance(inp, dict) else None
    ti = ti if isinstance(ti, dict) else {}
    out = []
    fp = ti.get("file_path")
    if isinstance(fp, str) and fp:
        out.append(fp)
    edits = ti.get("edits")
    if isinstance(edits, list):
        out += [e["file_path"] for e in edits if isinstance(e, dict) and isinstance(e.get("file_path"), str)]
    if out:
        return out, True
    if inp.get("tool_name") == "apply_patch" or any(isinstance(ti.get(k), str) and "*** Begin Patch" in ti[k] for k in ("command", "input", "patch")):
        for k in ("command", "input", "patch"):
            if isinstance(ti.get(k), str):
                out = parse_patch_paths(ti[k])
                if out:
                    return out, True
        return [], False
    return [], True


def select_files(root, paths, exts):
    """Existing files inside root (not in the installed layer: .agents, .claude, .codex) whose extension the lint covers;
    repo-relative."""
    real_root = os.path.realpath(root)
    keep = []
    for p in paths:
        ap = os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p))
        rel = os.path.relpath(ap, real_root)
        if rel.startswith("..") or rel.split(os.sep)[0] in OWNED_DIRS:
            continue
        if os.path.splitext(rel)[1].lower() not in exts or not os.path.isfile(ap):
            continue
        if rel not in keep:
            keep.append(rel)
    return keep


def _targets(manifest_paths):
    out = set(_REPO_TARGETS)
    for k in ("src", "tests"):
        for e in (manifest_paths or {}).get(k) or []:
            p = e.get("path") if isinstance(e, dict) else e
            if isinstance(p, str) and p.strip("./"):
                p = p.rstrip("/")
                out.update({p, p + "/", "./" + p, "./" + p + "/"})
    return out


def file_command(cmd, files, manifest_paths=None):
    """`cmd` run on `files` only (see the module docstring). `manifest_paths` is the agentive manifest's `paths` map."""
    quoted = " ".join(shlex.quote(f) for f in files)
    if _SHELL_JOIN.search(cmd) or any(f.startswith("-") for f in files):    # flags (`-- --fix`) keep the target
        return cmd + " " + quoted
    known, base = _targets(manifest_paths), cmd.rstrip()
    while True:
        m = re.search(r"\s(\S+)$", base)
        if not m or m.group(1) not in known:
            break
        base = base[:m.start()].rstrip()
    return base + " " + quoted
