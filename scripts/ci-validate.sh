#!/bin/sh
set -eu

python -m compileall -q app scripts

python -m unittest discover -s tests -p 'test_*.py'
