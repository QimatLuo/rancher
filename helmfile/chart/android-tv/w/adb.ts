import {
  catchError,
  defer,
  delay,
  EMPTY,
  iif,
  map,
  merge,
  Observable,
  of,
  pipe,
  switchMap,
  tap,
  throwError,
} from "rxjs";
import { log } from "./log.ts";

function diff(a: string, b: string) {
  return cmd(`compare -metric ae -fuzz 0% ${a} ${b} /tmp/compare.png`);
}

function click(x: string) {
  return pipe(
    tap(() => log(x)),
    switchMap(() => cmd("adb shell input keyevent DPAD_CENTER")),
    delay(5000),
  );
}

function cmd(input: string) {
  return of(input).pipe(
    tap((x) => {
      if (x.includes("screencap")) return;
      log(x);
    }),
    map(() => input.split(" ")),
    map(([x, ...args]) => new Deno.Command(x, { args })),
    switchMap((x) => x.output()),
    switchMap((x) =>
      iif(
        () => x.code === 0,
        defer(() => of(new TextDecoder().decode(x.stdout))),
        throwError(() => new TextDecoder().decode(x.stderr)),
      )
    ),
    tap((x) => {
      if (x === "") return;
      log(x);
    }),
  );
}

function screencap(): Observable<unknown> {
  const command = new Deno.Command("adb", {
    args: [
      "exec-out",
      "screencap",
      "-p",
    ],
    stdin: "piped",
    stdout: "piped",
  });
  const child = command.spawn();

  const y = child.stdout.pipeTo(
    Deno.openSync("/tmp/screencap.png", { write: true, create: true }).writable,
  );

  return defer(() => child.status).pipe(
    tap(() => child.stdin.close()),
    switchMap((x) =>
      iif(
        () => x.success,
        defer(() => y),
        cmd("adb connect 192.168.1.112").pipe(switchMap(() => screencap())),
      )
    ),
  );
}

export default defer(() => screencap()).pipe(
  switchMap(() =>
    diff("/tmp/screencap.png", "/sample/screencap.png").pipe(
      switchMap(() => EMPTY),
      catchError((e) => of(e)),
    )
  ),
  switchMap(() =>
    cmd("convert /tmp/screencap.png -crop 80x50+1550+950 /tmp/crop.png")
  ),
  switchMap(() =>
    merge(
      diff("/tmp/crop.png", "/sample/ignore.png").pipe(
        click("IGNORE"),
        catchError((e) => of(e)),
      ),
      diff("/tmp/crop.png", "/sample/next.png").pipe(
        click("NEXT"),
        catchError((e) => of(e)),
      ),
    )
  ),
);
