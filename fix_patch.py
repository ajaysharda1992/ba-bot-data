
# Repair: fix escaped quotes that landed OUTSIDE JS string literals in applyViewLiqHist
f = '/tmp/ba-dashboard/index.html'
s = open(f).read()

broken = 'toLocaleString(\\"en-IN\\",{timeZone:\\"Asia/Kolkata\\",day:\\"2-digit\\",month:\\"short\\",hour:\\"2-digit\\",minute:\\"2-digit\\",hour12:false})'
fixed  = 'toLocaleString("en-IN",{timeZone:"Asia/Kolkata",day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",hour12:false})'

n = s.count(broken)
if n == 0 and s.count(fixed) == 1:
    print('Already fixed - nothing to do.')
    raise SystemExit
assert n == 1, 'broken pattern found ' + str(n) + ' times'
open(f, 'w').write(s.replace(broken, fixed))
print('FIXED - toLocaleString quotes repaired')
