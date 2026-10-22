; NSIS skips its reinstall page under /S. Remove the previous package's owned
; files before copying the new payload, just as the interactive upgrade does.
; /UPDATE preserves application data. Do not recursively delete $INSTDIR:
; the per-user diagram cache can live beside the installed program.
!macro NSIS_HOOK_PREINSTALL
  ${If} ${Silent}
  ${AndIf} ${FileExists} "$INSTDIR\uninstall.exe"
    Push $R0
    Push $R1
    ReadRegStr $R1 SHCTX "${MANUPRODUCTKEY}" ""
    ${If} $R1 != $INSTDIR
      Pop $R1
      Pop $R0
      Abort "Existing uninstaller is not registered to this installation directory."
    ${EndIf}
    !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
    ClearErrors
    ExecWait '"$INSTDIR\uninstall.exe" /S /UPDATE _?=$INSTDIR' $R0
    ${If} ${Errors}
    ${OrIf} $R0 != 0
    ${OrIf} ${FileExists} "$INSTDIR\${MAINBINARYNAME}.exe"
      Pop $R1
      Pop $R0
      Abort "The previous PlateFold package could not be removed for upgrade."
    ${EndIf}
    Pop $R1
    Pop $R0
  ${EndIf}
!macroend
