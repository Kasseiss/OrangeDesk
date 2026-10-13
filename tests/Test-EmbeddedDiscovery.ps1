#requires -Version 5.1
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
# Compile the current source without rebuilding the distributable or running UIA queries.
. (Join-Path $taskRoot '源码\CombinedRuntime.ps1')
Initialize-Automation
$script:Checks=0
function Check([bool]$Condition,[string]$Message) {
    if (-not $Condition) { throw ('FAIL: '+$Message) }
    $script:Checks++
    Write-Host ('PASS '+$Message)
}
$fixtureDirectory=Join-Path $env:TEMP ('FastOrangeEmbeddedTest-'+[guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($fixtureDirectory)
$fixtureExe=Join-Path $fixtureDirectory 'WindowFixture.exe'
$fixtureSource=Join-Path $fixtureDirectory 'WindowFixture.cs'
$compilerOutput=Join-Path $fixtureDirectory 'compiler-output.txt'
$compilerError=Join-Path $fixtureDirectory 'compiler-error.txt'
$hostReady=Join-Path $fixtureDirectory 'host.json'
$targetReady=Join-Path $fixtureDirectory 'target.json'
$fixtureCode=@'
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
namespace FastOrangeEmbeddedTest {
    public static class WindowFixture {
        private delegate IntPtr WindowProc(IntPtr hwnd,uint message,IntPtr wparam,IntPtr lparam);
        [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)]
        private struct WindowClass {
            public uint size,style;
            public WindowProc proc;
            public int classExtra,windowExtra;
            public IntPtr instance,icon,cursor,background;
            public string menu,className;
            public IntPtr smallIcon;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct Message {
            public IntPtr hwnd;
            public uint message;
            public UIntPtr wparam;
            public IntPtr lparam;
            public uint time;
            public int x,y;
            public uint privateData;
        }
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] private static extern IntPtr GetModuleHandle(string name);
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] private static extern ushort RegisterClassEx(ref WindowClass value);
        [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern IntPtr CreateWindowEx(uint exstyle,string className,string title,uint style,int x,int y,int width,int height,IntPtr parent,IntPtr menu,IntPtr instance,IntPtr parameter);
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] private static extern IntPtr DefWindowProc(IntPtr hwnd,uint message,IntPtr wparam,IntPtr lparam);
        [DllImport("user32.dll")] private static extern bool DestroyWindow(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern int GetMessage(out Message message,IntPtr hwnd,uint min,uint max);
        [DllImport("user32.dll")] private static extern bool TranslateMessage(ref Message message);
        [DllImport("user32.dll")] private static extern IntPtr DispatchMessage(ref Message message);
        private static readonly WindowProc proc=DefWindowProc;
        private static readonly List<IntPtr> windows=new List<IntPtr>();
        private static readonly HashSet<string> classes=new HashSet<string>();
        private static IntPtr Create(string className,string title,IntPtr parent) {
            IntPtr instance=GetModuleHandle(null);
            if (classes.Add(className)) {
                WindowClass value=new WindowClass(); value.size=(uint)Marshal.SizeOf(typeof(WindowClass));
                value.proc=proc; value.instance=instance; value.className=className;
                if (RegisterClassEx(ref value)==0) throw new Exception("RegisterClassEx failed");
            }
            // Deliberately omit WS_VISIBLE: these are metadata fixtures, never user UI.
            uint style=parent==IntPtr.Zero ? 0x00CF0000u : 0x40000000u;
            IntPtr hwnd=CreateWindowEx(0,className,title,style,0,0,100,100,parent,IntPtr.Zero,instance,IntPtr.Zero);
            if (hwnd==IntPtr.Zero) throw new Exception("CreateWindowEx failed: "+Marshal.GetLastWin32Error());
            windows.Add(hwnd); return hwnd;
        }
        private static void Ready(string path,Dictionary<string,IntPtr> handles) {
            List<string> parts=new List<string>();
            foreach (KeyValuePair<string,IntPtr> item in handles) parts.Add("\""+item.Key+"\":"+item.Value.ToInt64());
            File.WriteAllText(path,"{"+String.Join(",",parts.ToArray())+"}");
        }
        public static void Main(string[] args) {
            try {
                Dictionary<string,IntPtr> handles=new Dictionary<string,IntPtr>();
                if (args[0]=="host") {
                    handles["Companion"]=Create("Qt699QWindowIcon","快橙伴航 - hidden test",IntPtr.Zero);
                    handles["Container"]=Create("Qt699QWindowIcon","Native container",handles["Companion"]);
                    handles["Companion2"]=Create("Qt699QWindowIcon","快橙伴航",IntPtr.Zero);
                    handles["WrongTitle"]=Create("Qt699QWindowIcon","Unrelated app",IntPtr.Zero);
                    handles["WrongClass"]=Create("NativeStubHost","快橙伴航",IntPtr.Zero);
                    handles["ForeignChild"]=Create("NativeStubHost","Foreign child",handles["Container"]);
                } else {
                    handles["Standalone"]=Create("OrangeTargetStub","Standalone target",IntPtr.Zero);
                    handles["Embedded"]=Create("OrangeTargetStub","Embedded target",new IntPtr(Int64.Parse(args[2])));
                    handles["Nested"]=Create("OrangeTargetStub","Native target child",handles["Embedded"]);
                    handles["Nested2"]=Create("OrangeTargetStub","Nested native target child",handles["Nested"]);
                    handles["Embedded2"]=Create("OrangeTargetStub","Second target root",new IntPtr(Int64.Parse(args[3])));
                    handles["WrongTitleChild"]=Create("OrangeTargetStub","Excluded child",new IntPtr(Int64.Parse(args[4])));
                    handles["WrongClassChild"]=Create("OrangeTargetStub","Excluded child",new IntPtr(Int64.Parse(args[5])));
                }
                Ready(args[1],handles);
                Message message;
                while (GetMessage(out message,IntPtr.Zero,0,0)>0) { TranslateMessage(ref message); DispatchMessage(ref message); }
            } finally {
                for (int i=windows.Count-1;i>=0;i--) DestroyWindow(windows[i]);
            }
        }
    }
}
'@
function Wait-Fixture($Process,[string]$ReadyPath) {
    $deadline=[DateTime]::UtcNow.AddSeconds(10)
    do {
        if ($Process.HasExited) { throw ('Fixture process exited: '+$Process.ExitCode) }
        if ([IO.File]::Exists($ReadyPath)) { return ([IO.File]::ReadAllText($ReadyPath) | ConvertFrom-Json) }
        Start-Sleep -Milliseconds 50
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Fixture did not create its hidden windows.'
}
$hostProcess=$null; $targetProcess=$null
try {
    [IO.File]::WriteAllText($fixtureSource,$fixtureCode,[Text.UTF8Encoding]::new($true))
    # .NET Framework keeps Add-Type output locked in its process; compile in a finite child.
    $compilerArguments='-NoLogo -NoProfile -Command "& { $ErrorActionPreference=''Stop''; Add-Type -Path '''+$fixtureSource.Replace("'","''")+''' -OutputAssembly '''+$fixtureExe.Replace("'","''")+''' -OutputType ConsoleApplication }"'
    $compilerProcess=Start-Process -FilePath $script:PowerShell -ArgumentList $compilerArguments -WindowStyle Hidden -RedirectStandardOutput $compilerOutput -RedirectStandardError $compilerError -Wait -PassThru
    try {
        if ($compilerProcess.ExitCode -ne 0) { throw ('Fixture compilation failed: '+[IO.File]::ReadAllText($compilerError)) }
    } finally { $compilerProcess.Dispose() }
    $hostProcess=Start-Process -FilePath $fixtureExe -ArgumentList @('host',('"'+$hostReady+'"')) -WindowStyle Hidden -PassThru
    $hostHandles=Wait-Fixture $hostProcess $hostReady
    $targetProcess=Start-Process -FilePath $fixtureExe -ArgumentList @('target',('"'+$targetReady+'"'),$hostHandles.Container,$hostHandles.Companion2,$hostHandles.WrongTitle,$hostHandles.WrongClass) -WindowStyle Hidden -PassThru
    $targetHandles=Wait-Fixture $targetProcess $targetReady
    $found=@([FastOrangeCombined.NativeWindows]::ForProcess($targetProcess.Id) | ForEach-Object { $_.ToInt64() })
    Check ($found -contains $targetHandles.Standalone) 'Original standalone top-level discovery is preserved'
    Check ($found -contains $targetHandles.Embedded -and $found -contains $targetHandles.Embedded2) 'Target roots are found under matching companion hosts and native containers'
    Check ($found -notcontains $targetHandles.Nested -and $found -notcontains $targetHandles.Nested2) 'Target native descendants do not create redundant UIA roots'
    Check ($found -notcontains $targetHandles.WrongTitleChild -and $found -notcontains $targetHandles.WrongClassChild) 'Both companion title and Qt class must match'
    Check ($found -notcontains $hostHandles.ForeignChild -and $found -notcontains $hostHandles.Companion) 'Other process windows are excluded'
    Check ($found.Count -ge 3 -and @($found | Sort-Object -Unique).Count -eq $found.Count) 'All target roots are distinct, including any system-created top-level windows'
    $companionMethod=[FastOrangeCombined.NativeWindows].GetMethod('CompanionFor',([Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Static))
    $embeddedHost=$companionMethod.Invoke($null,@([IntPtr]::new([long]$targetHandles.Embedded)))
    Check ($embeddedHost.ToInt64() -eq $hostHandles.Companion) 'Opening an embedded target resolves to its companion host'
    foreach ($key in @('Standalone','WrongTitleChild','WrongClassChild')) {
        $resolved=$companionMethod.Invoke($null,@([IntPtr]::new([long]$targetHandles.$key)))
        Check ($resolved -eq [IntPtr]::Zero) ('Opening '+$key+' retains original behavior')
    }
    Stop-Process -Id $targetProcess.Id -Force
    [void]$targetProcess.WaitForExit(3000)
    Check (@([FastOrangeCombined.NativeWindows]::ForProcess($targetProcess.Id)).Count -eq 0) 'Closing the original target removes all discovered handles'
    Write-Host ('PASS: '+$script:Checks+' assertions; hidden native fixtures only, no real UIA queries or clicks.')
} finally {
    foreach ($fixtureProcess in @($targetProcess,$hostProcess)) {
        if ($null -ne $fixtureProcess) {
            try {
                if (-not $fixtureProcess.HasExited) { Stop-Process -Id $fixtureProcess.Id -Force }
                [void]$fixtureProcess.WaitForExit(3000)
                $fixtureProcess.Dispose()
            } catch { }
        }
    }
    foreach ($fixtureFile in @($fixtureExe,$fixtureSource,$compilerOutput,$compilerError,$hostReady,$targetReady)) {
        if ([IO.File]::Exists($fixtureFile)) { [IO.File]::Delete($fixtureFile) }
    }
    if ([IO.Directory]::Exists($fixtureDirectory)) { [IO.Directory]::Delete($fixtureDirectory) }
}
