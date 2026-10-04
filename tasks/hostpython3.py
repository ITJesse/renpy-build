from renpybuild.model import task

version = "3.9.10"


@task(kind="host", pythons="3")
def unpack_hostpython(c):
    c.clean()

    c.var("version", version)
    c.run("tar xzf {{source}}/Python-{{version}}.tgz")

    c.chdir("Python-{{ version }}")


@task(kind="host", pythons="3")
def build_host(c):
    c.var("version", version)

    c.chdir("Python-{{ version }}")

    c.run("""./configure --prefix="{{ host }}" """)
    c.generate("{{ source }}/Python-{{ version }}-Setup.local", "Modules/Setup.local")

    c.run("""{{ make }} install""")

    # RenPyLinter: the config directory is named after the build machine
    # (config-3.9-x86_64-linux-gnu upstream, config-3.9-darwin on macOS).
    c.var("config_dir", next(c.path("{{ host }}/lib/python3.9").glob("config-3.9-*")))
    c.rmtree("{{ config_dir }}/Tools/")
    c.run("install -d {{ config_dir }}/Tools/")
    c.run("cp -a Tools/scripts {{ config_dir }}/Tools/scripts")
