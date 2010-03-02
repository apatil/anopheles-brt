import os,sys
import anopheles_query
from anopheles_query import *



for mod in ['env_data','validation_metrics','export_vectors','query_to_rec','brt_wrap']:
    try:
        exec('from %s import *'%mod)
    except ImportError:
        cls, inst, tb = sys.exc_info()
        print 'Failed to import %s. Error message:\n\t%s'%(mod,inst.message)
try:
    import utils
except ImportError:
    print 'Failed to import utils'