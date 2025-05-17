import {
  defer,
  iif,
  of,
  switchMap,
  tap,
  throwError,
  withLatestFrom,
} from "rxjs";
import { CmdOutput, Log } from "./di.ts";

export function cmd(input: string) {
  return of(input.split(" ")).pipe(
    tap(() => {
      if (input.includes("1920x685+0+0")) return;
      if (input.includes("76x76+1767+934")) return;
      if (input.endsWith("info:")) return;
      if (input.startsWith("rm")) return;
      Log.next(input);
    }),
    withLatestFrom(CmdOutput),
    switchMap(([[command, ...args], cmdOutput]) =>
      of(new Deno.Command(command, { args })).pipe(
        switchMap(cmdOutput),
        switchMap((x) =>
          iif(
            () => x.code === 0,
            defer(() => of(new TextDecoder().decode(x.stdout))),
            throwError(() => new TextDecoder().decode(x.stderr)),
          )
        ),
        tap((x) => {
          if (x === "") return;
          if (input.endsWith("info:")) return;
          Log.next(x);
        }),
      )
    ),
  );
}
