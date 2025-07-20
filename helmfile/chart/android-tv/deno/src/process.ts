import {
  buffer,
  catchError,
  debounceTime,
  EMPTY,
  filter,
  merge,
  repeat,
  share,
  Subject,
  switchMap,
  takeUntil,
  tap,
  withLatestFrom,
} from "rxjs";
import { Log } from "./di.ts";
import main, { connect } from "./adb.ts";

export const event = new Subject<string>();

const trigger = event.pipe(buffer(event.pipe(debounceTime(50))), share());

const start = trigger.pipe(filter((xs) => xs.includes("playing")));
const stop = merge(
  trigger.pipe(filter((xs) => xs.includes("off"))),
  trigger.pipe(filter((xs) => xs.includes("paused"))),
);

export default merge(
  event.pipe(
    filter((x) => x === "on"),
    switchMap(() => connect),
  ),
  start.pipe(
    withLatestFrom(Log),
    tap(([, log]) => log("start")),
    switchMap(() =>
      main.pipe(
        catchError((e) =>
          Log.pipe(
            tap((log) => log("error", e)),
            switchMap(() => EMPTY),
          )
        ),
        repeat(),
      )
    ),
    takeUntil(
      stop.pipe(
        withLatestFrom(Log),
        tap(([, log]) => log("stop")),
      ),
    ),
    repeat(),
  ),
);
