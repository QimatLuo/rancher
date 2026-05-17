#!/usr/bin/env bash

_paint_completion_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_paint_completion_script="${_paint_completion_dir}/paint.py"

_paint_targets() {
  local pybin
  pybin="$(command -v python3 || command -v python)"
  if [[ -z "${pybin}" ]]; then
    return 0
  fi

  "${pybin}" -c 'import importlib.util, sys
script_path = sys.argv[1]
spec = importlib.util.spec_from_file_location("paint_module", script_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(" ".join(module.target_map().keys()))' "${_paint_completion_script}" 2>/dev/null
}

_paint_complete_direct() {
  local cur
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"

  if [[ ${COMP_CWORD} -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "$(_paint_targets)" -- "${cur}") )
  fi
}

_paint_complete_python() {
  local cur script
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  script="${COMP_WORDS[1]}"

  if [[ ${COMP_CWORD} -eq 2 && "${script##*/}" == "paint.py" ]]; then
    COMPREPLY=( $(compgen -W "$(_paint_targets)" -- "${cur}") )
  fi
}

complete -F _paint_complete_direct paint.py
complete -o default -F _paint_complete_python python
complete -o default -F _paint_complete_python python3
