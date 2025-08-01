import {
  defer,
  delay,
  EMPTY,
  filter,
  iif,
  map,
  Observable,
  of,
  pipe,
  retry,
  switchMap,
  throwIfEmpty,
  timer,
} from "rxjs";

import { ocrProcess } from "./ocr.ts";
import { cmd } from "./command.ts";

export const connect = cmd("adb connect 192.168.1.112");

function click() {
  return pipe(
    switchMap(() => cmd("adb shell input keyevent DPAD_CENTER")),
    delay(3000),
  );
}

function screencap(): Observable<{
  filename: string;
  duration: number;
}> {
  let t: number;
  return defer(() => {
    t = Date.now();
    const command = new Deno.Command("adb", {
      args: [
        "exec-out",
        "screencap",
        "-p",
      ],
      stdin: "piped",
      stdout: "piped",
    });
    const child = command.spawn();

    child.stdout.pipeTo(
      Deno.openSync(`/tmp/${t}.png`, { write: true, create: true })
        .writable,
    );

    child.stdin.close();
    return child.status;
  }).pipe(
    filter((x) => x.success),
    map(() => ({
      filename: `/tmp/${t}.png`,
      duration: Date.now() - t,
    })),
    throwIfEmpty(() => new Error('screencap failed')),
    retry({
      delay: () => connect.pipe(delay(1000)),
    }),
  );
}

export default defer(() => screencap()).pipe(
  switchMap(
    ({ filename, duration }) =>
      ocrProcess(filename).pipe(
        switchMap((x) =>
          iif(
            () => x > 5,
            EMPTY,
            timer(x * 1000 - duration).pipe(
              click(),
            ),
          )
        ),
      ),
  ),
);
