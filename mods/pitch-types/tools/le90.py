# Dump LE90 database table schemas (DESC) and optionally records.
import struct,sys
def parse_desc(d):
    assert d[:4]==b'LE90'
    p=d.find(b'[DESC]')+6
    tables=[]
    while True:
        if d[p:p+6]==b'[DATA]': break
        name=d[p:p+16].split(b'\0')[0].decode(); size,nf=struct.unpack_from('<II',d,p+16); p+=24
        fields=[]
        for i in range(nf):
            fn=d[p:p+22].split(b'\0')[0].decode(); fid,ft,cnt,bs,pad=struct.unpack_from('<5H',d,p+22); p+=32
            fields.append((fn,fid,ft,cnt,bs))
        tables.append((name,size,fields))
    return tables,p
if __name__=='__main__':
    d=open(sys.argv[1],'rb').read()
    t,p=parse_desc(d)
    for name,size,fields in t:
        print('TABLE',name,'recsize',hex(size),len(fields))
        off=0
        for f in fields:
            print('   +%-4x %-22s id=%-3d type=%-2d count=%-3d bytes=%d'%(off,f[0],f[1],f[2],f[3],f[4])); off+=f[4]
    print('DATA at',hex(p))

def read_tables(d):
    """Parse DATA: returns {table: [records as dict]} for top-level tables; child tables are attached to parent records under '_children'."""
    tables,p=parse_desc(d)
    sch={t[0]:t[2] for t in tables}
    names={t[0].encode() for t in tables}
    def rec(name,p):
        r={}
        for fn,fid,ft,cnt,bs in sch[name]:
            raw=d[p:p+bs]; p+=bs
            if ft==1 and cnt>1 and fn.endswith('NAME'): r[fn]=raw.split(b'\0')[0].decode('latin1')
            elif ft==1: r[fn]=raw[0] if cnt==1 else list(raw)
            elif ft==2: r[fn]=struct.unpack('<%dH'%cnt,raw) if cnt>1 else struct.unpack('<H',raw)[0]
            elif ft==4: r[fn]=struct.unpack('<%dI'%cnt,raw) if cnt>1 else struct.unpack('<I',raw)[0]
            else: r[fn]=raw
        return r,p
    def isname(p):
        n=d[p:p+16].split(b'\0')[0]
        return n in names
    out={}
    assert d[p:p+6]==b'[DATA]'; p+=6
    while p<len(d):
        name=d[p:p+16].split(b'\0')[0].decode(); cnt=struct.unpack_from('<I',d,p+16)[0]; p+=20
        lst=[]
        for i in range(cnt):
            r,p=rec(name,p)
            ch={}
            # child tables follow the record
            while p<len(d) and isname(p):
                cn=d[p:p+16].split(b'\0')[0].decode()
                if cn in out or cn==name or (cn in ch): break
                # peek: a top-level table after the last record has many records; children have few
                cc=struct.unpack_from('<I',d,p+20-4)[0]
                if i==cnt-1 and cn not in ('D_PLYR_CONTRACT','D_PLYR_ANIMS','D_PLYR_AUDIO','D_PLYR_FLAGS','D_QUIRKS','D_STANCE_DATA','D_PLAYER_ACC','D_PLAYER_APP'): break
                q=p+20; sub=[]
                for j in range(cc):
                    rr,q=rec(cn,q); sub.append(rr)
                ch[cn]=sub; p=q
            if ch: r['_children']=ch
            lst.append(r)
        out[name]=lst
    return out
