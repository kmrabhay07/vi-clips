import {spawn} from 'node:child_process';
import {createWriteStream} from 'node:fs';
import {mkdir,stat} from 'node:fs/promises';
import {pipeline} from 'node:stream/promises';
import {Transform} from 'node:stream';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const app=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const envFile=process.env.VI_ENV_FILE||path.join(app,'env','.env');
process.loadEnvFile(envFile);
const required=name=>{const value=process.env[name];if(!value)throw new Error('Set '+name+' in the private environment file');return value};
const target=required('SSH_USER')+'@'+required('SSH_HOST');
const remoteRoot=required('VI_DEPLOY_ROOT').replace(/\/$/,'');
if(!/^[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.:-]*$/.test(target)||!/^\/[A-Za-z0-9_./-]+$/.test(remoteRoot)||remoteRoot==='/'||path.posix.normalize(remoteRoot)!==remoteRoot)throw new Error('Invalid private deployment address or path');
const project=process.env.COMPOSE_PROJECT_NAME||'vi-clips';
const compose=['compose','--env-file',envFile];
const backup=path.join(app,'env','backups');
await mkdir(backup,{recursive:true});
const sshArgs=['-i',required('SSH_IDENTITY_FILE'),'-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes','-o',`UserKnownHostsFile=${required('SSH_KNOWN_HOSTS_FILE')}`];
function child(command,args){return spawn(command,args,{cwd:app,stdio:['pipe','pipe','inherit']})}
function ended(process){return new Promise((resolve,reject)=>{process.on('error',reject);process.on('close',code=>code===0?resolve():reject(new Error(`Process failed: ${code}`)))})}
const dump=child('docker',[...compose,'exec','-T','db','pg_dump','-U',process.env.POSTGRES_USER||'clips','-d',process.env.POSTGRES_DB||'clips','-Fc','--no-owner','--no-acl']);
dump.stdin.end();
await Promise.all([pipeline(dump.stdout,createWriteStream(path.join(backup,'library.dump'),{mode:0o600})),ended(dump)]);
console.log(`Database snapshot saved (${(await stat(path.join(backup,'library.dump'))).size} bytes).`);
const uploadDump=spawn('scp',[...sshArgs,path.join(backup,'library.dump'),target+':'+remoteRoot+'/backups/library.dump'],{cwd:app,stdio:'inherit'});
await ended(uploadDump);
const script="import tarfile,sys; from pathlib import Path; root=Path('/data'); archive=tarfile.open(fileobj=sys.stdout.buffer,mode='w|'); [archive.add(p,arcname=p.relative_to(root),recursive=False) for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink() and p.relative_to(root).parts[0]!='models']; archive.close()";
const media=child('docker',['run','--rm','--network','none','-v',(process.env.LOCAL_MEDIA_VOLUME||project+'_media')+':/data:ro',process.env.LOCAL_API_IMAGE||project+'-api:latest','python','-c',script]);
media.stdin.end();
const receiver=child('ssh',[...sshArgs,target,'umask 077; cat > '+remoteRoot+'/backups/media.tar']);
receiver.stdout.resume();
let bytes=0,last=Date.now();
const progress=new Transform({transform(chunk,encoding,callback){bytes+=chunk.length;if(Date.now()-last>=30000){console.log(`Media transferred: ${(bytes/1024**2).toFixed(0)} MiB`);last=Date.now()}callback(null,chunk)}});
await Promise.all([pipeline(media.stdout,progress,receiver.stdin),ended(media),ended(receiver)]);
console.log(`Media transfer complete: ${bytes} bytes. Cached speech models are excluded and will download on OCI.`);
