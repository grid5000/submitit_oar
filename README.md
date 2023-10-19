# What is submitit_oar?
submitit_oar is an [Oar](https://oar.imag.fr/) plugin for [Submitit](https://github.com/facebookincubator/submitit), which is a Python 3.8+ lightweight toolbox for submitting Python functions for computation within a [Slurm](https://slurm.schedmd.com/quickstart.html) cluster. It basically wraps submission and provide access to results, logs and more.

# How to install submitit_oar?
Quick install, in a virtualenv/conda environment where `pip` is installed (check `which pip`):
- latest release:
```
pip install submitit_oar==1.0.0
```
- from source:
```
pip install git+https://github.com/ychiat35/submitit_oar@main#egg=submitit_oar
```

# How to use the submitit_oar plugin?
From inside an environment with `submitit` and `submitit_oar` installed, you can run this addition example:

```python
import submitit

def add(a, b):
    return a + b

# logs are dumped in the folder
executor = submitit.AutoExecutor(folder="log_test", cluster="oar")

job_addition = executor.submit(add, 5, 7)  # will compute add(5, 7)
output = job_addition.result()  # waits for completion and returns output
# if ever the job failed, result() will raise an error with the corresponding trace
print('job_addition output: ', output)
assert output == 12
```

You can try running this above example with a breakpoint at https://github.com/facebookincubator/submitit/blob/main/submitit/core/plugins.py#L33, to be sure that Submitit correctly finds the submitit_oar plugin.
