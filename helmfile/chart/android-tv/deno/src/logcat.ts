import { filter, tap } from "rxjs";
import { logcat } from "./adb.ts";
import { Log } from "./di.ts";

export const clicked = logcat.pipe(
  filter((x) => x.includes("keyCode=KEYCODE_DPAD_CENTER")),
  filter((x) => x.includes("action=ACTION_UP")),
  tap((x) => Log.next(x)),
);

export const noMore = logcat.pipe(
  filter((x) => x.includes("youtube")),
  filter((x) => x.includes("onPlaybackStateChanged")),
  filter((x) => x.includes("with no new data")),
  tap((x) => Log.next(x)),
);
