import { assertEquals } from "@std/assert";
import { TestScheduler } from "rxjs/testing";
import { concatMap, from, map, of } from "rxjs";

import { CmdOutput, Log } from "../src/di.ts";
import { ocr } from "../src/ocr.ts";

CmdOutput.next((x) => of(x.outputSync()));
Log.next(() => {});

const testScheduler = new TestScheduler((actual, expected) =>
  assertEquals(actual, expected)
);

Deno.test("simple test", () => {
  testScheduler.run(({ expectObservable }) => {
    const testCase = from([
      "no_ignore",
      "can_ignore",
      "next",
      "1",
      "4",
      "5",
      "6",
      "8",
      "10",
      "13",
      "14",
      "16",
      "18",
      "19",
      "21",
      "23",
    ]).pipe(
      map((x) => `${Deno.cwd()}/test/img/${x}.png`),
      concatMap(ocr),
    );
    expectObservable(testCase).toBe(
      "(014568adegijln|)",
      {
        ...Object.fromEntries(
          Array(10).fill(0).map((x, i) => x + i).map((x) => [`${x}`, x]),
        ),
        ...Object.fromEntries(
          Array(26).fill(10).map((
            x,
            i,
          ) => [String.fromCharCode(97 + i), x + i]),
        ),
      },
    );
  });
});
