import {
  defer,
  delay,
  filter,
  iif,
  Observable,
  switchMap,
  takeUntil,
  tap,
  timeout,
  timer,
} from "rxjs";
import { click, logcat, screencap } from "./adb.ts";
import { ocrProcess } from "./ocr.ts";
import { CmdOutput, Log } from "./di.ts";

CmdOutput.next((x) => x.output());
Log.subscribe(console.log);

const shiftTime = +(Deno.env.get("POD_NAME")?.slice(-1) || 0) * 1000;

const main: Observable<string> = defer(() => screencap()).pipe(
  switchMap(({ filename, duration }) =>
    ocrProcess(filename).pipe(
      tap((x) => Log.next(x)),
      switchMap((x) =>
        iif(() => x > 5, main, timer(x * 1000 - duration).pipe(click()))
      ),
    )
  ),
);

const clicked = logcat.pipe(
  filter((x) => x.includes("keyCode=KEYCODE_DPAD_CENTER")),
  filter((x) => x.includes("action=ACTION_UP")),
  tap((x) => Log.next(x)),
);

logcat
  .pipe(
    timeout(1000 * 60 * 60),
    filter((x) => x.includes("youtube")),
    filter((x) => x.includes("onPlaybackStateChanged")),
    filter((x) => x.includes("actions=51")),
    delay(shiftTime),
    tap((x) => Log.next(x)),
    switchMap(() => main.pipe(takeUntil(clicked))),
  )
  .subscribe({
    complete: () => Log.next("complete"),
    error: (x) => Log.next("error", x),
    next: (x) => Log.next("next", x),
  });
