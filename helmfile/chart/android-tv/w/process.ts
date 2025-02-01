import {
  buffer,
  debounceTime,
  partition,
  repeat,
  share,
  Subject,
  switchMap,
  takeUntil,
} from "rxjs";
import main from "./adb.ts";

export const event = new Subject<string>();

const trigger = event.pipe(
  buffer(event.pipe(
    debounceTime(50),
  )),
  share(),
);

const [start, stop] = partition(trigger, (xs) => xs.includes("/playing") && !xs.includes("/off"));

export default start.pipe(
  switchMap(() => main.pipe(repeat())),
  takeUntil(stop),
  repeat(),
);
