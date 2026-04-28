from setuptools import setup, find_packages
setup(
    name="sbsp",
    version="0.1.0",
    packages=find_packages(),
    install_requires=["pyroute2", "pytest", "pytest-asyncio"],
    entry_points={
        "console_scripts": [
            "sbspd=sbsp.daemon.main:main",
            "sbsp-show=sbsp.cli.show:main",
        ]
    },
)
