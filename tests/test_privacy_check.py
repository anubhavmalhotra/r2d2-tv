import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('privacy_rules',ROOT/'tools/privacy_check.py')
privacy=importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)

# Every personal-looking value is assembled at run time so this file passes the scanner.
AT='@'
HEX=hashlib.sha256(b'synthetic fixture').hexdigest()
LAT=12.3456
LON=-45.6789


def codes(text, name='x.py'):
    return privacy.issues(name,text.encode() if isinstance(text,str) else text)


class PathRuleTests(unittest.TestCase):
    def test_home_paths_including_file_urls(self):
        for text in ('file://'+'/Users/'+'person/a','file://'+'/home/'+'person/a','file://localhost'+'/Users/'+'person',
                     'see '+'/Users/'+'person','cd ~'+'/Documents/x','~'+'/Desktop/a','~'+'/Downloads/a','~'+'/Library/a'):
            self.assertIn('personal-home-path',codes(text),text)
        for text in ('src'+'/Users/'+'person','/srv'+'/home/'+'person','/home/'+'{{SPARK_USER}}/runtime','~/project/a'):
            self.assertNotIn('personal-home-path',codes(text),text)


    def test_forbidden_names_parts_and_suffixes(self):
        for name in ('tv/server.crt','a.cer','a.der','a.p12','a.pfx','a.jks','a.keystore','id_rsa','id_ed25519.pub',
                     'tv-ssap-client-key.json','ssap-pin.json','client-key.txt','tv-pairing.json','DeviceIdentity.json',
                     'credentials.prod.json','secrets.toml','a.private.yaml','.npmrc','.netrc','.env.production',
                     'experimental/x.py'):
            self.assertIn('private-or-generated-path',codes(b'',name),name)
        for name in ('voice/tv/device_identity.js','voice/tv/test_device_identity.js','README.md','tools/privacy_check.py'):
            self.assertEqual(codes(b'',name),[],name)


class SecretRuleTests(unittest.TestCase):
    def test_private_key_headers(self):
        for kind in ('','RSA ','DSA ','EC ','OPENSSH ','ENCRYPTED '):
            self.assertIn('private-key',codes('-----BEGIN '+kind+'PRIVATE KEY-----'),kind)
        self.assertIn('private-key',codes('-----BEGIN '+'PGP PRIVATE KEY BLOCK-----'))
        self.assertIn('private-key',codes('PuTTY-User-'+'Key-File-3: ssh-ed25519'))
        for header in ('PUBLIC KEY','CERTIFICATE','PGP PUBLIC KEY BLOCK'):
            self.assertNotIn('private-key',codes('-----BEGIN '+header+'-----'),header)

    def test_hex_credentials_in_identifiers(self):
        for text in ('api_key = "'+HEX+'"','self.auth_token='+"'"+HEX+"'",'{"clientSecret": "'+HEX+'"}',
                     'password: '+HEX,'Authorization: Bearer '+HEX,'hmac_key="'+HEX+'"','CRED_ID='+HEX[:32]):
            self.assertIn('hex-credential',codes(text),text)

    def test_digest_paths_and_fixtures_are_not_credentials(self):
        for text in ('keyHash = "'+HEX+'"','token_sha256: '+HEX,'pinned_key_digest="'+HEX+'"','auth_mac='+HEX,
                     '"revision": "'+HEX[:40]+'"','"js/trusted-tokens.js": "'+HEX+'"',"{'StarfishKeyFilter.qml':'"+HEX+"'}",
                     'token="'+'ab'*32+'"','token="'+HEX[:31]+'"','key="'+HEX+'ff'*40+'"','monotonic="'+HEX+'"'):
            self.assertNotIn('hex-credential',codes(text),text)
        raw=json.dumps({'browser':{'key':'a1'*32}})
        self.assertEqual(codes(raw,'tv/home/catalog.js.in'),['embedded-capability'])


class PersonalDataRuleTests(unittest.TestCase):
    def test_personal_email(self):
        for text in ('person'+AT+'gmail.com','Signed-off-by: A <a.b+c'+AT+'mail.co.uk>','mailto:x'+AT+'company.io'):
            self.assertIn('personal-email',codes(text),text)

    def test_placeholder_and_public_addresses(self):
        for text in ('1+name'+AT+'users.noreply.github.com','a'+AT+'example.com','a'+AT+'sub.example.org',
                     'a'+AT+'example.net','a'+AT+'example.invalid','a'+AT+'host.test','a'+AT+'evil.example',
                     'noreply'+AT+'service.example','git'+AT+'github.com:owner/repo.git','https://user'+AT+'duckduckgo.com/',
                     'https://evil.example'+AT+'duckduckgo.com/','load pkg'+AT+'1.2.3-beta.min.js','name'+AT+'localhost'):
            self.assertNotIn('personal-email',codes(text),text)

    def test_postal_codes(self):
        digits='1234'+'5'
        for text in ('{"zip": "'+digits+'"}','zip='+digits,"postalCode: '"+digits+"-6789'",'ZIP code '+digits,
                     'owner_postcode='+digits,'homeZip='+digits):
            self.assertIn('postal-code',codes(text),text)
        for text in ("zip:'00000'",'gzip: '+digits,'zip='+digits[:4],'zip(a, b)','postal=POSTAL-SENTINEL'):
            self.assertNotIn('postal-code',codes(text),text)

    def test_coordinates(self):
        for text in ('loc="%s,%s"'%(LAT,LON),'(%s, %s)'%(LAT,LON),'{"lat": %s, "lon": %s}'%(LAT,LON),
                     'latitude=%s,longitude=%s'%(LAT,LON),'"lat":"%s","lng":"%s"'%(LAT,LON)):
            self.assertIn('location-coordinates',codes(text),text)

    def test_synthetic_coordinates_and_drawing_data(self):
        for text in ("ipinfo('40.0049,-100.0049')","'40.1234,-100.5678'","'0.0010,-0.0040'",'points_path(40.004, -99.996)',
                     "'/points/40.000,-100.000'",'cubic-bezier(0.215, 0.610, 0.355, 1.000)','d="M 12.345,67.890 L 13.456,68.901"',
                     '<polygon points="1.234,5.678 9.012,3.456"/>','range(123.456,200.123)','version 1.2.3,4.5.6',
                     '"lat": %s'%LAT,'"lon": %s'%LON):
            self.assertNotIn('location-coordinates',codes(text),text)

    def test_weather_station_ids(self):
        for text in ('"stations": ["K'+'QRS"]','metar?ids=K'+'QRS','icaoId: K'+'QRS','station\n  "K'+'QRS"'):
            self.assertIn('weather-station-id',codes(text),text)
        for text in ('stations: KAAA,KBBB,KCCC,KDDD,KEEE,KZZZ',"('icaoId', 'KZZZ')",'K'+'QRS was here',' '.join('K'+w for w in ('EYS','ILL','IND'))):
            self.assertNotIn('weather-station-id',codes(text),text)


class AddressRuleTests(unittest.TestCase):
    def test_cgnat_addresses(self):
        for text in ('100.'+'100.5.6','100.'+'64.10.20','http://100.'+'127.1.1:8080'):
            self.assertIn('cgnat-ipv4',codes(text),text)
        for text in ('100.64.0.0/10','100.64.0.1','100.127.255.255','100.128.0.1','100.63.255.255','1.100.64.5.6'):
            self.assertNotIn('cgnat-ipv4',codes(text),text)

    def test_ipv6_unique_local_and_link_local(self):
        for text in ('fd12'+':3456:789a::1','[fe80'+'::1c2b:3a4d]','FD00'+':1:2:3:4:5:6:7','fe80'+'::1%en0','fd12'+':3456::/48'):
            self.assertIn('private-ipv6',codes(text),text)
        for text in ('fc00::/7','fe80::/10','2001:db8::1','fd12','coffee:fd12'):
            self.assertNotIn('private-ipv6',codes(text),text)


class ScannerModeTests(unittest.TestCase):
    def git(self, directory, *args):
        env=dict(os.environ,GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_NOSYSTEM='1')
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture'+AT+'example.com',
                        '-c','commit.gpgsign=false',*args],cwd=directory,env=env,check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    def run_main(self, directory, *argv):
        original=privacy.ROOT
        privacy.ROOT=Path(directory)
        out=io.StringIO()
        try:
            with contextlib.redirect_stdout(out):status=privacy.main(list(argv))
        finally:privacy.ROOT=original
        return status,out.getvalue()

    def test_history_reports_codes_paths_and_commits_but_never_values(self):
        secret_path='/Users/'+'zq-owner/secret-project'
        with tempfile.TemporaryDirectory() as directory:
            self.git(directory,'init','-q')
            Path(directory,'notes.txt').write_text('see '+secret_path+'\n')
            self.git(directory,'add','notes.txt')
            self.git(directory,'commit','-q','-m','add notes from '+secret_path)
            Path(directory,'notes.txt').write_text('clean\n')
            self.git(directory,'commit','-q','-am','clean notes')
            status,output=self.run_main(directory)
            self.assertEqual(status,0)
            status,output=self.run_main(directory,'--history')
            self.assertEqual(status,1)
            self.assertNotIn('zq-owner',output)
            self.assertNotIn('secret-project',output)
            report=json.loads(output)
            self.assertEqual(report['commits'],2)
            blobs=[f for f in report['findings'] if 'blob' in f]
            commits=[f for f in report['findings'] if 'commit' in f]
            self.assertEqual([(f['path'],f['codes']) for f in blobs],[('notes.txt',['personal-home-path'])])
            self.assertEqual([f['codes'] for f in commits],[['personal-home-path']])
            self.assertEqual(len(commits[0]['commit']),40)

    def test_unknown_argument_refused(self):
        status,output=self.run_main(ROOT,'--worktree')
        self.assertEqual(status,2)
        self.assertFalse(json.loads(output)['passed'])


if __name__=='__main__':unittest.main()
