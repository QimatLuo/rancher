import {
  combineLatestWith,
  defaultIfEmpty,
  defer,
  filter,
  iif,
  lastValueFrom,
  map,
  of,
  switchMap,
  tap,
  throwError,
} from "rxjs";
import main, { event } from "./process.ts";
import { CmdOutput, Log } from "./di.ts";

CmdOutput.next((x) => x.output());
Log.next(log);

main.subscribe({
  next: (x) => log(new Date().toJSON(), "next", x),
  error: (x) => log("error", x),
  complete: () => log("complete"),
});

export default {
  fetch: (req: Request) => {
    log(req.method, req.url);
    const u = new URL(req.url);
    event.next(u.pathname);

    const body = of(req.body).pipe(
      filter(Boolean),
      switchMap(() => req.text()),
      map((x) => x.split(" "))
    );

    const output = of(req).pipe(
      filter((x) => x.method === "POST"),
      filter(() => u.pathname === "/cmd"),
      combineLatestWith(body),
      tap((x) => log(x)),
      map(([, [x, ...args]]) => new Deno.Command(x, { args })),
      switchMap((x) => x.output()),
      switchMap((x) =>
        iif(
          () => x.code === 0,
          defer(() => of(new TextDecoder().decode(x.stdout))),
          throwError(() => new TextDecoder().decode(x.stderr))
        )
      ),
      map((x) => Response.json(x))
    );

    return lastValueFrom(
      output.pipe(
        defaultIfEmpty(
          Response.json(
            {
              method: req.method,
              url: req.url,
            },
            {
              status: 404,
            }
          )
        )
      )
    ).catch((x) => {
      console.error(x);
      return Response.json(x, {
        status: 400,
      });
    });
  },
};

function log(...any: unknown[]) {
  console.log(new Date().toJSON(), ...any);
}
