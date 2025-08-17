import {
  delay,
  EMPTY,
  exhaustMap,
  expand,
  filter,
  iif,
  last,
  map,
  merge,
  mergeMap,
  Observable,
  of,
  retry,
  switchMap,
  takeUntil,
  tap,
  throwIfEmpty,
  timer,
} from "rxjs";
import { click, logcat, screencap } from "./adb.ts";
import { ocrProcess } from "./ocr.ts";
import { CmdOutput, Log } from "./di.ts";

CmdOutput.next((x) => x.output());
Log.subscribe(console.log);

const shiftTime = +(Deno.env.get("POD_NAME")?.slice(-1) || 0) * 1000;

const main: Observable<string> = of({ duration: NaN, number: NaN }).pipe(
  expand((last) =>
    iif(
      () => last.number <= 5,
      EMPTY,
      screencap().pipe(
        switchMap(({ filename, duration }) =>
          ocrProcess(filename).pipe(map((x) => ({ number: x, duration })))
        ),
        throwIfEmpty(() => "NaN"),
        retry(),
      ),
    )
  ),
  filter((x) => !isNaN(x.number)),
  mergeMap((x) => timer(x.number * 1000 - x.duration)),
  last(),
  click(),
);

const clicked = logcat.pipe(
  filter((x) => x.includes("keyCode=KEYCODE_DPAD_CENTER")),
  filter((x) => x.includes("action=ACTION_UP")),
  tap((x) => Log.next(x)),
);

const noMore = logcat.pipe(
  filter((x) => x.includes("youtube")),
  filter((x) => x.includes("onPlaybackStateChanged")),
  filter((x) => x.includes("with no new data")),
  tap((x) => Log.next(x)),
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
        takeUntil(
          merge(clicked, timer(1000 * 5).pipe(switchMap(() => noMore))),
        ),
      )
    ),
  )
  .subscribe({
    complete: () => Log.next("complete"),
    error: (x) => Log.next(["error", x]),
    next: (x) => Log.next(["next", x]),
  });
