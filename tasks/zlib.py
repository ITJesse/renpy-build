from renpybuild.context import Context
from renpybuild.task import task

version = "1.2.11"


@task()
def unpack(c: Context):
    c.clean()

    c.var("version", version)
    c.run("tar xzf {{source}}/zlib-{{version}}.tar.gz")

    # RenPyLinter: current Apple SDKs define TARGET_OS_MAC, so zlib 1.2.11
    # turns fdopen() into NULL and breaks <stdio.h>. zlib 1.3.1 drops that.
    c.chdir("zlib-{{version}}")
    c.patch("renpylinter/zlib-1.2.11-apple-fdopen.diff")


@task()
def build(c: Context):
    c.var("version", version)
    c.chdir("zlib-{{version}}")
    c.run("{{configure}} {{ configure_cross }} --static --prefix={{install}}")
    c.run("{{ make }}")
    c.run("make install")


@task(platforms="web", pythons="3")
def build_web(c: Context):
    c.run("embuilder build zlib")
