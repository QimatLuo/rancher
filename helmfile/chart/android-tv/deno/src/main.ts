import {
  defer,
  delay,
  exhaustMap,
  filter,
  first,
  last,
  map,
  merge,
  mergeMap,
  repeat,
  retry,
  switchMap,
  takeUntil,
  takeWhile,
  tap,
  throwIfEmpty,
  timer,
} from "rxjs";
import { click, logcat, screencap } from "./adb.ts";
import { ocrProcess } from "./ocr.ts";
import { CmdOutput, Log } from "./di.ts";
import { noMore } from "./logcat.ts";

CmdOutput.next((x) => x.output());
Log.subscribe(console.log);

const shiftTime = (+(Deno.env.get("POD_NAME")?.slice(-1) || 0) * 1000) || 0;

const main = defer(() =>
  screencap().pipe(
    switchMap(({ filename, duration }) =>
      ocrProcess(filename).pipe(map((x) => ({ number: x, duration })))
    ),
    throwIfEmpty(() => "cannot parse screenshot"),
    retry(),
    repeat(),
  )
).pipe(
  filter((x) => !isNaN(x.number)),
  takeWhile((x) => x.number > 5, true),
  mergeMap((x) => timer(x.number * 1000 - x.duration)),
  last(),
  click(),
);

const adStart = logcat.pipe(
  filter((x) => x.includes("youtube")),
  filter((x) => x.includes("onPlaybackStateChanged")),
  filter((x) => x.includes("actions=51")),
  tap((x) => Log.next(x)),
);

adStart
  .pipe(
    delay(shiftTime),
    tap(() => Log.next("main")),
    exhaustMap(() =>
      main.pipe(
        repeat(),
        takeUntil(
          merge(timer(1000 * 5).pipe(switchMap(() => noMore))),
        ),
      )
    ),
  )
  .subscribe({
    complete: () => Log.next("complete"),
    error: (x) => Log.next(["error", x]),
    next: (x) => Log.next(["next", x]),
  });
