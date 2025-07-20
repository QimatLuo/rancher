import {
  defer,
  delay,
  filter,
  map,
  Observable,
  pipe,
  retry,
  Subject,
  switchMap,
  throwIfEmpty,
} from "rxjs";

import { cmd } from "./command.ts";

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
    retry({
      delay: () => connect.pipe(delay(1000)),
    }),
  );
}

const logcatSubject = new Subject();

export const logcat = new Observable<string>((s) => {
  const command = new Deno.Command("adb", {
    args: ["logcat"],
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
});
