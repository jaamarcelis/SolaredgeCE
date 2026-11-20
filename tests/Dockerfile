# Testomgeving voor de integratietests. Home Assistant draait niet op Windows (fcntl), dus op Windows:
#   docker build -t solaredge-ha-tests -f tests/Dockerfile .
#   docker run --rm -v "${PWD}:/work" solaredge-ha-tests
FROM python:3.14-slim

RUN pip install --no-cache-dir pytest-homeassistant-custom-component aioresponses pycognito ruff

WORKDIR /work
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
