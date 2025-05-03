import { defer, filter, map, switchMap, tap } from "rxjs";

import { cmd } from "./command.ts";

export function ocr(input: string) {
  return cropRawToCircle(input).pipe(
    switchMap((circle) =>
      willIgnore(circle).pipe(
        tap((x) => {
          if (x) return;
          cmd(`rm ${input}`).subscribe();
        }),
        filter(Boolean),
        switchMap(() => cropCircleToNumber(circle)),
      )
    ),
    switchMap(pictureToNumber),
    filter((x) => !isNaN(x)),
    switchMap((n) =>
      isNext(input).pipe(
        map((x) => x ? 0 : n),
      )
    ),
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

function isNext(input: string) {
  return defer(() =>
    cmd(`convert ${input} -format "%[pixel:p{1600,970}]" info:`)
  ).pipe(
    map((x) => x === '"srgba(241,241,241,1)"'),
  );
}
