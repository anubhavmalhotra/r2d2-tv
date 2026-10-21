"""Compile the actual compatibility decision; no firmware or target calls."""
from pathlib import Path
import os, shutil, subprocess, tempfile, unittest

HERE = Path(__file__).resolve().parent
PREFIX = r'''
#include <cassert>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <initializer_list>
static int kind=0,calls=0,reads=0,closes=0;
static void fixture(struct stat*s){memset(s,0,sizeof(*s));s->st_dev=1;s->st_ino=20;s->st_mode=S_IFREG|0700;s->st_nlink=1;}
static int local_lstat(const char*,struct stat*s){
 fixture(s);
 if(kind==1)s->st_mode=S_IFLNK|0700;
 if(kind==2)s->st_nlink=2;
 if(kind==3)s->st_size=1;
 if(kind==4)s->st_uid=1;
 if(kind==5)s->st_gid=1;
 if(kind==6)s->st_mode|=0020;
 if((kind==7||kind==8)&&++reads==2)s->st_ino=21;
 if(kind==10){errno=ENOENT;return -1;}
 return 0;
}
static int local_fstat(int,struct stat*s){fixture(s);if(kind==7)s->st_ino=21;if(kind==9)s->st_mode=S_IFCHR|0600;return 0;}
static int local_close(int){++closes;return 0;}
static int local_open(const char*p,int flags,...){
 ++calls;
 if(!strcmp(p,"/tmp/voiceinput_hidraw.lock"))assert(flags==(O_RDWR|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC));
 return 42;
}
static void*dlsym(void*,const char*){return reinterpret_cast<void*>(local_open);}
#define RTLD_NEXT nullptr
#define lstat local_lstat
#define fstat local_fstat
#define close local_close
'''
SUFFIX = r'''
int main(){
 const char*p="/tmp/voiceinput_hidraw.lock";
 assert(own_open("open",-1,p,O_RDWR|O_CREAT,0700,false)==42&&calls==1);
 for(kind=1;kind<=8;++kind){calls=reads=closes=0;assert(own_open("open",-1,p,O_RDWR|O_CREAT,0700,false)==-1);if(kind<=6)assert(calls==0);if(kind>=7)assert(closes==1);}
 kind=10;calls=0;assert(own_open("open",-1,p,O_RDWR|O_CREAT,0700,false)==-1&&calls==0);
 kind=0;calls=0;
 for(int flags:{O_RDWR,O_RDWR|O_CREAT|O_TRUNC,O_RDWR|O_CREAT|O_APPEND})assert(own_open("open",-1,p,flags,0700,false)==-1&&calls==0);
 assert(own_open("open",-1,p,O_RDWR|O_CREAT,0600,false)==-1&&calls==0);
 assert(own_open("open",-1,"/tmp/another-file",O_RDWR|O_CREAT,0700,false)==-1&&calls==0);
 assert(own_open("openat",AT_FDCWD,p,O_RDWR|O_CREAT,0700,true)==-1&&calls==0);
 kind=9;calls=0;assert(own_open("open",-1,"/dev/random",O_RDWR,0,false)==-1&&calls==0);
 assert(own_open("open",-1,"/dev/hidraw1",O_RDWR,0,false)==42&&calls==1);
 kind=0;calls=closes=0;assert(own_open("open",-1,"/dev/hidraw1",O_RDWR,0,false)==-1&&calls==1&&closes==1);
 calls=0;assert(own_open("open",-1,"/tmp/ordinary-read",O_RDONLY,0,false)==42&&calls==1);
 return 0;
}
'''


def decision(source):
    return source[source.index('bool writing('):source.index('\n}\nextern "C" bool LSCall')]


class StartupLock(unittest.TestCase):
    def compile_run(self, header, source):
        compiler = os.environ.get('CXX') or shutil.which('c++')
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'case.cpp').write_text(PREFIX+header+source+SUFFIX)
            built = subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror',
                '-Wno-unused-function', str(root/'case.cpp'), '-o', str(root/'case')],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stderr)
            return subprocess.run([str(root/'case')], capture_output=True, timeout=2)

    def test_actual_current_decision(self):
        result = self.compile_run((HERE/'startup_lock.hpp').read_text(),
                                  decision((HERE/'owned_provider.cpp').read_text()))
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_removed_exception_reproduces_vendor_startup_refusal(self):
        source = decision((HERE/'owned_provider.cpp').read_text())
        begin = source.index(' // Compatibility for the pinned original main')
        end = source.index(' if(write&&', begin)
        # Compile the pre-fix production decision with the positive fixture. A
        # missing exception must fail the actual request assertion, not imports.
        source = source[:begin]+source[end:]
        result = self.compile_run('', source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'assert', result.stderr.lower())

    def test_assertion_sensitive_regressions(self):
        header = (HERE/'startup_lock.hpp').read_text()
        source = decision((HERE/'owned_provider.cpp').read_text())
        mutations = [
            ('create-reintroduced', 'header', 'open_file(path,O_RDWR|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC)', 'open_file(path,flags,mode)'),
            ('opened-race', 'header', '!same_lock(before,opened)||', ''),
            ('post-race', 'header', '||!same_lock(opened,after)', ''),
            ('wrong-path', 'header', '!strcmp(path,"/tmp/voiceinput_hidraw.lock")', 'true'),
            ('truncation-request', 'header', 'flags==(O_RDWR|O_CREAT)', '(flags&(O_RDWR|O_CREAT))==(O_RDWR|O_CREAT)'),
            ('wrong-mode', 'header', '&&mode==0700', '&&mode>=0'),
            ('foreign-file', 'header', 's.st_uid==0&&', ''),
            ('broad-write', 'source', 'if(write&&(!hid_path(path)||(flags&(O_CREAT|O_TRUNC|O_APPEND))))', 'if(false)'),
        ]
        for name, target, original, replacement in mutations:
            with self.subTest(name=name):
                h,s = header,source
                self.assertEqual((h if target=='header' else s).count(original), 1)
                if target=='header':h=h.replace(original,replacement)
                else:s=s.replace(original,replacement)
                # Keep a removed assertion's parameter visibly used: compilation
                # must pass, and the executable assertion must reject the mutant.
                if name=='wrong-mode':h=h.replace('&&mode>=0', '&&((void)mode,true)')
                result = self.compile_run(h,s)
                self.assertNotEqual(result.returncode, 0, name)
                self.assertIn(b'assert', result.stderr.lower(), name)


if __name__ == '__main__':
    unittest.main()
