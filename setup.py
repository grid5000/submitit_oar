from setuptools import setup

setup(
    name="submitit_oar",
    version="1.0.0",
    install_requires=["submitit==1.5.1"],
    entry_points={
        "submitit": "\n".join(
            [
                "",
                "executor = submitit_oar.oar:OarExecutor",
                "job_environment = submitit_oar.oar:OarJobEnvironment",
                "",
            ]
        )
    },
    zip_safe=False,
)