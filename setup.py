from setuptools import setup

setup(
    name="submitit_oar",
    install_requires=["submitit>=1.4.6"],
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