FROM denoland/deno
RUN apt update && apt install android-tools-adb imagemagick tesseract-ocr -y
COPY ./deno.json /deno.json
RUN deno install