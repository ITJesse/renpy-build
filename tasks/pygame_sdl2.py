from renpybuild.context import Context
from renpybuild.task import task, annotator


def ios_mode(c: Context):
    """
    RenPyLinter: setup.py asks sdl2-config for flags (libsdl2-dev on
    upstream's Linux host). An iOS-only build uses pygame_sdl2's own iOS mode,
    which links SDL2 by name and needs no host SDL2.
    """

    if c.platform == "ios":
        c.env("PYGAME_SDL2_IOS", "1")


@task(kind="host-python", pythons="3", platforms="all", always=True)
def gen_static3(c: Context):

    c.chdir("{{ pygame_sdl2 }}")
    c.env("PYGAME_SDL2_STATIC", "1")
    ios_mode(c)
    c.run("{{ hostpython }} setup.py generate")

@task(kind="python", platforms="all", pythons="3", always=True)
def install3(c: Context):
    ios_mode(c)
    c.run("{{ hostpython }} {{ pygame_sdl2 }}/setup.py install --single-version-externally-managed --record files.txt --no-compile --no-extensions")
    c.run("{{ hostpython }} {{ pygame_sdl2 }}/install_headers.py {{ install }}")
