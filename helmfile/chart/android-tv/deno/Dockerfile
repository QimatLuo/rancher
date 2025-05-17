FROM denoland/deno
RUN apt update && apt install android-tools-adb curl imagemagick tesseract-ocr -y
COPY ./deno.json /deno.json
RUN deno install