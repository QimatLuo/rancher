import {
  defer,
  delay,
  filter,
  map,
  Observable,
  pipe,
  retry,
  share,
  Subject,
  switchMap,
  throwIfEmpty,
} from "rxjs";

import { cmd } from "./command.ts";
import { Log } from "./di.ts";

export const connect = cmd("adb connect 192.168.1.112");
export const kill = cmd("adb kill-server");

export function click() {
  return pipe(
    switchMap(() => cmd("adb shell input keyevent DPAD_CENTER")),
    delay(3000),
  );
}

export function screencap(): Observable<{
  filename: string;
  duration: number;
}> {
  let t: number;
  return defer(() => {
    t = Date.now();
    const command = new Deno.Command("adb", {
      args: ["exec-out", "screencap", "-p"],
      stdin: "piped",
      stdout: "piped",
    });
    const child = command.spawn();

    child.stdout.pipeTo(
      Deno.openSync(`/tmp/${t}.png`, { write: true, create: true }).writable,
    );

    child.stdin.close();
    return child.status;
  }).pipe(
    filter((x) => x.success),
    map(() => ({
      filename: `/tmp/${t}.png`,
      duration: Date.now() - t,
    })),
    throwIfEmpty(() => new Error("screencap failed")),
    retry(),
  );
}

const logcatSubject = new Subject();
let subscribed = false;

export const logcat = new Observable<string>((s) => {
  const t = formatDateInTimezone(new Date(), 8);
  const cmd = "adb";
  const args = ["logcat", "-T", t];
  Log.next(["adb"].concat(args).join(" "));

  if (subscribed) return logcatSubject;
  subscribed = true;

  const command = new Deno.Command(cmd, {
    args,
    stdin: "piped",
    stdout: "piped",
  });
  const process = command.spawn();

  (async (reader) => {
    while (true) {
      const { done, value } = await reader.read();

      new TextDecoder()
        .decode(value)
        .split("\n")
        .forEach((x) => s.next(x));

      if (done) {
        s.complete();
        return;
      }
    }
  })(process.stdout.getReader());

  return logcatSubject;
}).pipe(
  share({
    resetOnRefCountZero: false,
  }),
);

function formatDateInTimezone(
  date: Date,
  targetTimezoneOffset: number,
): string {
  // Convert current date to target timezone
  const offsetDifference = targetTimezoneOffset - date.getTimezoneOffset() / 60;
  const targetDate = new Date(
    date.getTime() + offsetDifference * 60 * 60 * 1000,
  );

  // Extract various components of the date
  const year = targetDate.getFullYear();
  const month = (targetDate.getMonth() + 1).toString().padStart(2, "0");
  const day = targetDate.getDate().toString().padStart(2, "0");

  // Use Intl.DateTimeFormat for time parts
  const timeFormatter = new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

  // Get time in the format hh:mm:ss
  const [{ value: hour }, , { value: minute }, , { value: second }] =
    timeFormatter.formatToParts(targetDate);

  // Get milliseconds
  const millis = targetDate.getMilliseconds().toString().padStart(3, "0");

  // Manually combine the parts
  return `${year}-${month}-${day} ${hour}:${minute}:${second}.${millis}`;
}
