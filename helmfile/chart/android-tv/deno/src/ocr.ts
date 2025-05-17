import {
  defer,
  filter,
  first,
  iif,
  map,
  mergeMap,
  of,
  pipe,
  switchMap,
  tap,
} from "rxjs";

import { cmd } from "./command.ts";

export function ocrProcess(input: string) {
  const a = cropRawToCircle(input).pipe(
    switchMap((circle) =>
      willIgnore(circle).pipe(
        removeIfNot(input),
        switchMap(() => cropCircleToNumber(circle)),
      )
    ),
    switchMap(pictureToNumber),
    filter((x) => !isNaN(x)),
  );

  return cropUpperBlackSection(input).pipe(
    switchMap(isAllBlack),
    removeIfNot(input),
    switchMap(() => cropRawToIgnore(input)),
    switchMap(toMd5),
    switchMap(
      (x) => iif(() => isIgnore(x) || isNext(x), of(0), a),
    ),
  );
}

function removeIfNot(input: string) {
  return pipe(
    tap((x) => {
      if (x) return;
      cmd(`rm ${input}`).subscribe();
    }),
    filter(Boolean),
  );
}

function cropCircleToNumber(input: string) {
  const output = "/tmp/number.jpg";
  return cmd(
    `convert ${input} -crop 40x30+19+23 -negate ${output}`,
  ).pipe(
    map(() => output),
  );
}

function cropRawToCircle(input: string) {
  const output = "/tmp/circle.jpg";
  return cmd(
    `convert ${input} -crop 76x76+1767+934 -fuzz 10% -fill #fff +opaque #000 ${output}`,
  ).pipe(
    map(() => output),
  );
}

function cropRawToIgnore(input: string) {
  const output = "/tmp/ignore.jpg";
  return cmd(
    `convert ${input} -crop 76x76+1590+934 ${output}`,
  ).pipe(
    map(() => output),
  );
}

function cropUpperBlackSection(input: string) {
  const output = "/tmp/upper.jpg";
  return cmd(
    `convert ${input} -crop 1920x685+0+0 ${output}`,
  ).pipe(
    map(() => output),
  );
}

function toMd5(input: string) {
  return cmd(
    `md5sum ${input}`,
  ).pipe(
    mergeMap((x) => x.split(" ")),
    first(),
  );
}

function isAllBlack(input: string) {
  return cmd(
    `convert ${input} -format "%[fx:mean]" info:`,
  ).pipe(
    map((x) => x === '"0"'),
  );
}

function pictureToNumber(input: string) {
  return defer(() =>
    cmd(
      `tesseract ${input} stdout --psm 7 -c tessedit_char_whitelist=0123456789`,
    )
  )
    .pipe(
      map((x) => parseInt(x, 10)),
    );
}

function willIgnore(input: string) {
  return defer(() => cmd(`convert ${input} -format "%[pixel:p{40,5}]" info:`))
    .pipe(
      map((x) => x.replaceAll(/[a-z(,)"]/g, "")),
      map((x) => parseInt(x)),
      map((x) => x > 255),
    );
}

function isIgnore(x: string) {
  return [
    "71fa4960292e673572864142415e6c05",
    "80bfb355e6fd7a7c3872f15c7a1297fb",
  ]
    .includes(x);
}
function isNext(x: string) {
  return [
    "b0bd314d6e2a95c4b8df5986deff4a2d",
    "48aa26867664b4568c1dc9b22d1ae898",
  ]
    .includes(x);
}
