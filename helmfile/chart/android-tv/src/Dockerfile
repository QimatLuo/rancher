FROM ubuntu

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		adb \
		ffmpeg \
		python3 \
		python3-opencv \
	&& rm -rf /var/lib/apt/lists/*
