FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Runtime libs needed by OpenCV in slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        adb \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Install latest OpenCV for Python from PyPI.
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --upgrade opencv-python reactivex

# Verify OpenCV is importable at build time.
RUN python -c "import cv2; print('OpenCV version:', cv2.__version__)"

WORKDIR /app

CMD ["python3"]
