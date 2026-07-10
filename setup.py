from setuptools import find_packages, setup

setup(
    name="metagpt",
    version="1.1.0",
    packages=find_packages(exclude=["tests*", "ztest*"]),
    python_requires=">=3.9",
)
