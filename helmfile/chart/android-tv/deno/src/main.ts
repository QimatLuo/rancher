import {
  defer,
  filter,
  iif,
  Observable,
  switchMap,
  tap,
  timeout,
  timer,
} from "rxjs";
import { click, logcat, screencap } from "./adb.ts";
import { ocrProcess } from "./ocr.ts";
import { CmdOutput, Log } from "./di.ts";

CmdOutput.next((x) => x.output());
Log.next(log);
function log(...any: unknown[]) {
  console.log(new Date().toJSON(), ...any);
}

const main: Observable<string> = defer(() => screencap()).pipe(
  switchMap(({ filename, duration }) =>
    ocrProcess(filename).pipe(
      tap((x) => log(x)),
      switchMap((x) =>
        iif(() => x > 5, main, timer(x * 1000 - duration).pipe(click()))
      ),
    )
  ),
);

logcat
  .pipe(
    timeout(1000 * 60 * 60),
    filter((x) => x.includes("youtube")),
    filter((x) => x.includes("onPlaybackStateChanged")),
    filter((x) => x.includes("actions=51")),
    switchMap(() => main),
  )
  .subscribe({
    complete: () => log("complete"),
    error: (x) => log("error", x),
    next: (x) => log("next", x),
  });
