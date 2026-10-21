"""Inert pipe peer for the actual JS receiver; fixed public test key only."""
import base64,json,sys
import stop_control as C

class Signals:
    def __init__(self):self.state=dict(released=False,cancelled=False,closed=False,reason=None)
    def snapshot(self):return dict(self.state)

def main():
    signals=Signals();now=[0.0]
    control=C.StopControl(signals,Signals,bytearray([7])*32,'a'*48,clock=lambda:now[0])
    for line in sys.stdin:
        try:
            row=json.loads(line);now[0]=row['now']/1000
            if row.get('close'):
                control.close();out={'summary':control.summary()}
            else:
                if row.get('state'):signals.state.update(row['state'])
                raw=base64.b64decode(row['body'],validate=True)
                result=control.respond(raw)
                out={'reply':base64.b64encode(result).decode(),'counter':control._counter,'end':control._end}
        except ValueError as error:out={'error':str(error),'counter':control._counter,'end':control._end}
        print(json.dumps(out,separators=(',',':')),flush=True)

if __name__=='__main__':main()
