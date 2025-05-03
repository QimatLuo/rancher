import {
  buffer,
  debounceTime,
  filter,
  merge,
  partition,
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

const trigger = event.pipe(
  filter((x) => x !== "/idle"),
  buffer(event.pipe(debounceTime(50))),
  share(),
);

const [start, stop] = partition(
  trigger,
  (xs) => xs.includes("/playing") && !xs.includes("/off"),
);

export default merge(
  event.pipe(
    filter((x) => x === "/on"),
    switchMap(() => connect),
  ),
  start.pipe(
    withLatestFrom(Log),
    tap(([, log]) => log("start")),
    switchMap(() => main.pipe(repeat())),
    takeUntil(
      stop.pipe(
        withLatestFrom(Log),
        tap(([, log]) => log("stop")),
      ),
    ),
    repeat(),
  ),
);
