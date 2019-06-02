import io
import os
import re
from setuptools import find_packages, setup

# Package arguments
PACKAGE_NAME = "keras_efficientnets"
SHORT_DESCRIPION = "Keras implementation of EfficientNets of any configuration."
URL = "https://github.com/titu1994/keras-efficientnets"
LICENCE = 'MIT'

# Extra requirements and configs
EXTRA_REQUIREMENTS = {
    'test': ['pytest'],
}
REQUIRED_PYTHON = ">=3.0.0"  # Can be None, or a string value

# Signature arguments
AUTHOR = "Somshubra Majumdar"
EMAIL = "titu1994@gmail.com"


###############################################################

def get_version():
    """Return package version as listed in `__version__` in `init.py`."""
    init_py = open(os.path.join(PACKAGE_NAME, '__init__.py')).read()
    return re.search("__version__ = ['\"]([^'\"]+)['\"]", init_py).group(1)


base_path = os.path.abspath(os.path.dirname(__file__))

try:
    with open(os.path.join(base_path, 'requirements.txt'), encoding='utf-8') as f:
        REQUIREMENTS = f.read().split('\n')

except Exception:
    REQUIREMENTS = []


try:
    with io.open(os.path.join(base_path, 'README.md'), encoding='utf-8') as f:
        LONG_DESCRIPTION = '\n' + f.read()

except FileNotFoundError:
    LONG_DESCRIPTION = SHORT_DESCRIPION


setup(
    name=PACKAGE_NAME,
    version=get_version(),
    packages=find_packages(),
    url=URL,
    download_url=URL,
    python_requires=REQUIRED_PYTHON,
    license=LICENCE,
    author=AUTHOR,
    author_email=EMAIL,
    description=SHORT_DESCRIPION,
    long_description=LONG_DESCRIPTION,
    long_description_content_type='text/markdown',
    install_requires=REQUIREMENTS,
    extras_require=EXTRA_REQUIREMENTS,
    classifiers=(
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.5",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Topic :: Software Development :: Libraries",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ),
    test_suite="tests",
)