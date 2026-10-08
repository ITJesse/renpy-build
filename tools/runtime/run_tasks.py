#!/usr/bin/env python3
"""Run a subset of a renpy-build checkout's tasks for iOS with Xcode.

This runs inside the checkout's own Python environment (the one its
``build.py``/``build.sh`` would use), so the task modules import exactly as
upstream expects. It replaces upstream's platform loop with a fixed iOS target
list and a module allow-list, and installs the Xcode toolchain on every
Context. Task bodies are never modified here.

Usage: run_tasks.py --root SRC --python 3 --archs arm64,sim-arm64 MODULE...
"""

import argparse
import inspect
import os
import shutil
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import xcode_toolchain  # noqa: E402


def link_sdk(context):
    """Stand-in for upstream's iOS ``cross`` task, which unpacks an SDK tarball."""

    sdk, _ = xcode_toolchain.IOS_TARGETS[context.arch]
    cross = context.tmp / f"cross.ios-{context.arch}"
    cross.mkdir(parents=True, exist_ok=True)
    link = cross / "sdk"
    target = xcode_toolchain.sdk_path(sdk)
    if link.is_symlink() or link.exists():
        if os.readlink(link) == target:
            return
        link.unlink()
    link.symlink_to(target)


# Sources a task writes into the Ren'Py lib tree, compiles and deletes (the
# pythonlib task's site.py / sitecustomize.py / sysconfig.py, made from
# runtime/ files plus appended lines). pybytecode.py recompiles every bundled
# module from its real source, so these are kept here, relative to renpy/lib.
GENERATED_SOURCES = Path("tmp") / "rpl-generated-sources"


def keep_deleted_sources(context_class, root):
    """Wrap Context.unlink so a .py removed from renpy/lib is copied first."""

    unlink = context_class.unlink
    distlib = (root / "renpy" / "lib").resolve()
    kept = root / GENERATED_SOURCES

    def wrapper(self, fn):
        path = Path(self.path(fn))
        if path.suffix == ".py" and path.is_file():
            try:
                rel = path.resolve().relative_to(distlib)
            except ValueError:
                rel = None
            if rel is not None:
                (kept / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, kept / rel)
        return unlink(self, fn)

    context_class.unlink = wrapper


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--python", required=True, choices=["2", "3"])
    ap.add_argument("--archs", default="arm64,sim-arm64")
    ap.add_argument("modules", nargs="+")
    args = ap.parse_args()

    root = args.root.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))

    import renpybuild.run

    if (root / "renpybuild" / "task.py").exists():
        # 7.8 and 8.1+: renpybuild.task / renpybuild.context.
        import renpybuild.task as framework
        from renpybuild.context import Context as UpstreamContext

        if "python" in inspect.signature(UpstreamContext.__init__).parameters:
            def make_context(arch):
                return UpstreamContext("ios", arch, args.python, root, build_args)
        else:
            # 8.6+: Python 3 only; the Context no longer takes a Python version.
            if args.python != "3":
                raise SystemExit("This checkout's framework builds Python 3 only.")

            def make_context(arch):
                return UpstreamContext("ios", arch, root, build_args)
    else:
        # 7.5 / 8.0: renpybuild.model, whose Context also takes the tmp,
        # pygame_sdl2 and renpy directories (build.py's defaults).
        import renpybuild.model as framework

        def make_context(arch):
            return framework.Context("ios", arch, args.python, root, root / "tmp", root / "pygame_sdl2",
                                     root / "renpy", build_args)

    shutil.rmtree(root / GENERATED_SOURCES, ignore_errors=True)
    keep_deleted_sources(framework.Context if framework.__name__ == "renpybuild.model" else UpstreamContext, root)

    upstream_environment = renpybuild.run.build_environment

    def build_environment(c):
        upstream_environment(c)
        xcode_toolchain.apply(c)

    renpybuild.run.build_environment = build_environment

    import tasks  # noqa: F401  (registers tasks in upstream order)

    wanted = set(args.modules)
    known = {t.name for t in framework.tasks}
    unknown = wanted - known
    if unknown:
        raise SystemExit(f"Unknown task modules for this checkout: {sorted(unknown)}")

    archs = [a.strip() for a in args.archs.split(",") if a.strip()]
    for arch in archs:
        if arch not in xcode_toolchain.IOS_TARGETS:
            raise SystemExit(f"Unsupported iOS arch {arch}")

    # The same switches upstream's argparse provides to tasks.
    build_args = types.SimpleNamespace(nostrip=False, sdl=False, experimental=False, stop=None,
                                       platforms="ios", archs=",".join(archs), pythons=args.python,
                                       tmp=str(root / "tmp"), pygame_sdl2=str(root / "pygame_sdl2"),
                                       renpy=str(root / "renpy"))

    for task in framework.tasks:
        if task.name not in wanted:
            continue

        for arch in archs:
            context = make_context(arch)

            if task.kind == "host" and framework.__name__ == "renpybuild.model":
                # As renpybuild.task does: host tasks build for the build
                # machine. In upstream's full builds the old framework ran
                # them with the first platform (linux), never with iOS
                # settings such as -framework MetalANGLE.
                context.platform = "host"
                context.arch = "host"

            if task.kind == "cross" and task.name == "toolchain":
                # Replaces the SDK tarball unpack; mark it complete like upstream would.
                link_sdk(context)
                continue

            task.run(context)

    print("\nSelected tasks finished successfully.")


if __name__ == "__main__":
    main()
