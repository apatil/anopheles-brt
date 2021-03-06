import numpy as np
import os
from env_data import extract_environment
from query_to_rec import sites_as_ndarray
import map_utils
import hashlib
import warnings
import matplotlib
import pymc as pm
import cPickle
from treetran import treetran
matplotlib.use('pdf')
import multiprocessing
from pylab import rec2csv, csv2rec

def get_names(layer_names, glob_name, glob_channels):
    "Utility"
    return map(lambda ch: str.lower(os.path.basename(ch)),layer_names) \
                + map(lambda ch: str.lower(os.path.basename(glob_name)) + '_' + str(ch), glob_channels)

def sanitize_species_name(s):
    return s.replace(' ','.').replace('-','.')

def df_to_ra(d):
    "Converts an R data fram to a NumPy record array."
    a = [np.array(c) for c in d.iter_column()]
    n = np.array(d.colnames)
    return np.rec.fromarrays(a, names=','.join(n))

def get_pseudoabsences(eo, buffer_width, n_pseudoabsences, layer_names, glob_name):
    fname = hashlib.sha1(cPickle.dumps(eo)+'_'+str(buffer_width)+'_'+str(n_pseudoabsences)).hexdigest()+'.npy'
    if fname in os.listdir('anopheles-caches'):
        pseudoabsences = np.load(os.path.join('anopheles-caches', fname))
    else:
        if buffer_width >= 0:
            buff = eo.buffer(buffer_width)
            diff_buffer = buff.difference(eo)
        elif buffer_width == -1:
            diff_buffer = eo
        else:
            raise ValueError, 'Buffer width is negative, but not -1.'
        
        if len(layer_names)>0:
            template = layer_names[0]
        else:
            template = glob_name

        lon, lat, test_raster, rtype = map_utils.import_raster(*os.path.split(template)[::-1])
        # Right the raster
        test_raster = map_utils.grid_convert(test_raster,'y-x+','x+y+')
        # Convert lower-left coords to centroids
        lon += (lon[1]-lon[0])/2.
        lat += (lat[1]-lat[0])/2.
    
        def testfn(lon_test,lat_test,r=test_raster,lon=lon,lat=lat):
            lon_ind = np.argmin(np.abs(lon-lon_test))
            lat_ind = np.argmin(np.abs(lat-lat_test))
            return True-test_raster.mask[lon_ind,lat_ind]
    
        pseudoabsences = np.vstack(map_utils.shapefile_utils.multipoly_sample(n_pseudoabsences, diff_buffer, test=testfn)).T
        if not np.all([testfn(l1,l2) for l1,l2 in pseudoabsences]):
            raise ValueError, 'Test failed for some pseudoabsences.'
        np.save(os.path.join('anopheles-caches',fname), pseudoabsences)
        
    return pseudoabsences
    
    
def sites_and_env(session, species, layer_names, glob_name, glob_channels, buffer_width, n_pseudoabsences, dblock=None, simdata=False):
    """
    Queries the DB to get a list of locations. Writes it out along with matching 
    extractions of the requested layers to a temporary csv file, which serves the 
    dual purpose of caching the extraction and making it easier to get data into 
    the BRT package.
    """

    breaks, x, found, zero, others_found, multipoints, eo = sites_as_ndarray(session, species)
    
    if simdata:
        print 'Process %i simulating presences for species %s.'%(multiprocessing.current_process().ident,species[1])
        x = get_pseudoabsences(eo, -1, n_pseudoabsences, layer_names, glob_name)
        found = np.ones(n_pseudoabsences)
        

    fname = hashlib.sha1(str(x)+found.tostring()+\
            glob_name+'channel'.join([str(i) for i in glob_channels])+\
            'layer'.join(layer_names)).hexdigest()+'.csv'
            
    pseudoabsences = get_pseudoabsences(eo, buffer_width, n_pseudoabsences, layer_names, glob_name)        
            
    x_found = x[np.where(found)]

    x = np.vstack((x_found, pseudoabsences))
    found = np.concatenate((np.ones(len(x_found)), np.zeros(n_pseudoabsences)))

    if fname in os.listdir('anopheles-caches'):
        pass
    else:

        # Makes list of (key, value) tuples
        env_layers = map(lambda ln: extract_environment(ln, x, lock=dblock), layer_names)\
                + map(lambda ch: (os.path.basename(glob_name)+'_'+str(ch), extract_environment(glob_name,x,\
                    postproc=lambda d: d==ch, id_=ch, lock=dblock)[1]), glob_channels)

        arrays = [(found>0).astype('int')] + [l[1] for l in env_layers]
        names = ['found'] + [l[0] for l in env_layers]

        data = np.rec.fromarrays(arrays, names=','.join(names))
        nancheck = np.array([np.any(np.isnan(row.tolist())) for row in data])
        if np.any(nancheck):
            print 'There were some NaNs in the data, probably points in the sea'

        singletons = 0
        for e in env_layers:
            if len(set(e[1][np.where(True-np.isnan(e[1]))]))==1:
                singletons += 1
        if singletons == len(env_layers):
            raise ValueError, 'All environmental layer evaluations contained only single values.'
        
        data = data[np.where(True-nancheck)]
        rec2csv(data, os.path.join('anopheles-caches',fname))

    return fname, pseudoabsences, x

def maybe_array(a):
    try:
        return np.array(a)
    except ValueError:
        return a

def unpack_gbm_object(brt_results, *names):
    """
    Allows retrieval of elements in the R vector brt_results
    by column name.
    """
    namelist = np.array(brt_results.names)
    return map(lambda n: maybe_array(brt_results[np.where(namelist==n)[0][0]]), names)
    
def print_gbm_object(brt_results, *names):
    namelist = np.array(brt_results.names)
    return map(lambda n: str(brt_results[np.where(namelist==n)[0][0]]), names)

def unpack_brt_trees(brt_results, layer_names, glob_name, glob_channels):
    """
    Returns a dictionary whose keys are the predictors used. The values of the 
    dictionary are record arrays giving the split points and node values 
    associated with the corresponding predictors.
    """
    all_names = get_names(layer_names, glob_name, glob_channels)
    
    tree_matrix = unpack_gbm_object(brt_results, 'trees')[0]
    nice_trees = []
    for split_var, split_code_pred, left_node, right_node, missing_node, error_reduction, weight, prediction in tree_matrix:
        new_tree = {'variable': all_names[int(split_var[0])],
                    'split_loc': split_code_pred[0]}
        for i in xrange(1,len(split_var)):
            if i==left_node[0]:
                new_tree['left_val'] = split_code_pred[int(i)]
            elif i==right_node[0]:
                new_tree['right_val'] = split_code_pred[int(i)]
        nice_trees.append(new_tree)

    nice_tree_dict = {}
    for v in all_names:
        v_array = map(lambda t: [t['split_loc'], t['left_val'], t['right_val']], filter(lambda t: t['variable']==v, nice_trees))
        nice_tree_dict[v] = np.rec.fromarrays(np.array(v_array).T, names='split_loc,left_val,right_val') if v_array else None
                
    return nice_tree_dict

def brt(fname, species_name, gbm_opts):
    """
    Takes the name of a CSV file containing a data frame and a dict
    of options for gbm.step, runs gbm.step, and returns the results.
    """
    from rpy2.robjects import r
    import anopheles_brt
    r.source(os.path.join(anopheles_brt.__path__[0],'brt.functions.R'))
    
    heads = file(os.path.join('anopheles-caches',fname)).readline().split(',')
    base_argstr = 'data=read.csv("anopheles-caches/%s"), gbm.x=2:%i, gbm.y=1, family="bernoulli", silent=TRUE'%(fname, len(heads))
    opt_argstr = ', '.join([base_argstr] + map(lambda t: '%s=%s'%t, gbm_opts.iteritems()))

    varname = sanitize_species_name(species_name)

    brt_fname = hashlib.sha1(opt_argstr).hexdigest()+'.r'
    if brt_fname in os.listdir('anopheles-caches'):
        r('load')(os.path.join('anopheles-caches', brt_fname))
        return r(varname)
    else:
        r('%s<-gbm.step(%s)'%(varname,opt_argstr))
        if str(r(varname))=='NULL':
            raise ValueError, 'gbm.step returned NULL'
        r('save(%s, file="%s")'%(varname,os.path.join('anopheles-caches', brt_fname)))
        return r(varname)

class brt_evaluator(object):
    """
    A lexical closure. Once created, takes predictive variables
    as a dictionary as an argument and returns a prediction on the
    corresponding grid.
    """
    def __init__(self, nice_tree_dict, intercept):
        self.nice_tree_dict = dict(map(lambda t: (str.lower(t[0]), t[1]), nice_tree_dict.iteritems()))
        self.intercept = intercept
    def __call__(self, pred_vars):
        if set(pred_vars.keys()) != set(self.nice_tree_dict.keys()):
            raise ValueError, "You haven't supplied all the predictors."
        out = np.empty(len(pred_vars.values()[0]))
        out.fill(self.intercept)
        N = 0.
        for n, trees in self.nice_tree_dict.iteritems():
            if trees is None:
                continue
            N += len(trees)
            treetran(trees.split_loc, trees.left_val, trees.right_val, pred_vars[n], out)
        return out

def brt_doublecheck(fname, brt_evaluator, brt_results):
    """
    Computes the 'fit' element of a gbm.object and compares it
    with that stored in the gbm.object.
    """
    ures = unpack_gbm_object(brt_results, 'fit')
    data = csv2rec(os.path.join('anopheles-caches', fname))
    ddict = dict([(k, data[k]) for k in data.dtype.names[1:]])
    out = brt_evaluator(ddict)

    print np.abs(out-ures).max()

def get_result_dir(species_name):
    "Utility"
    result_dirname = ('%s-results'%sanitize_species_name(species_name))
    try: 
        os.mkdir(result_dirname)
    except OSError:
        pass
    return result_dirname

def write_brt_results(brt_results, species_name, result_names):
    """
    Writes the actual R gbm.object containing the BRT results out to
    a results directory, and also some requested elements of it as
    flat text files.
    """
    from rpy2.robjects import r

    result_dirname = get_result_dir(species_name)
    varname = sanitize_species_name(species_name)
    r('save(%s, file="%s")'%(varname,os.path.join(result_dirname, 'gbm.object.r')))
    
    results = print_gbm_object(brt_results, *result_names)
    for n,v in zip(result_names, results):
        file(os.path.join(result_dirname, n+'.txt'),'w').write(str(v))
        
def subset_raster(r, llclati, llcloni, urclati, urcloni):
    r_ = map_utils.grid_convert(r,'y-x+','x+y+')
    return map_utils.grid_convert(r_[llcloni:urcloni,llclati:urclati],'x+y+','y-x+').astype('float32')
    
def trees_to_diagnostics(brt_evaluator, fname, species_name):
    """
    Takes the BRT evaluator and sees how well it does at predicting the training dataset.
    """

    from diagnostics import simple_assessments, roc, plot_roc_

    din = csv2rec(os.path.join('anopheles-caches',fname))
    found = din.found
    din = dict([(k,din[k]) for k in brt_evaluator.nice_tree_dict.iterkeys()])
    probs = pm.flib.invlogit(brt_evaluator(din))

    print 'Species %s: fraction %f correctly classified.'%(species_name, ((probs>.5)*found+(probs<.5)*(True-found)).sum()/float(len(probs)))

    result_dirname = get_result_dir(species_name)
    
    resdict = {}
    for f in simple_assessments:
        resdict[f.__name__] = f(probs>.5, found)

    pstack = np.array([pm.rbernoulli(probs) for i in xrange(10000)])
    fp, tp, AUC = roc(pstack, found)
    resdict['AUC'] = AUC
    
    fout=file(os.path.join(result_dirname,'simple-diagnostics.txt'),'w')
    fout.write('presences: %i\n'%found.sum())
    fout.write('absences: %i\n'%(True-found).sum())
    for k in resdict.iteritems():
        fout.write('%s: %s\n'%k)
    
    import pylab as pl
    pl.clf()
    plot_roc_(fp,tp,AUC)
    pl.savefig(os.path.join(result_dirname,'roc.pdf'))
    
    r = np.rec.fromarrays([fp,tp],names='false,true')
    rec2csv(r,os.path.join(result_dirname,'roc.csv'))


def trees_to_map(brt_evaluator, species_name, layer_names, glob_name, glob_channels, bbox, memlim = 4e9):
    """
    Makes maps and writes them out in flt format.
    """
    all_names = get_names(layer_names, glob_name, glob_channels)
    short_layer_names = all_names[:len(layer_names)]
    short_glob_names = all_names[len(layer_names):]
    result_dirname = get_result_dir(species_name)
    
    llclat,llclon,urclat,urclon = bbox
    
    rasters = {}

    n_rasters = len(layer_names)+len(glob_channels)
    lon,lat,glob,t = map_utils.import_raster(*os.path.split(glob_name)[::-1])
    
    orig_glob_shape = glob.shape
    
    llclati = np.where(lat>=llclat)[0][0]
    llcloni = np.where(lon>=llclon)[0][0]
    urclati = np.where(lat<=urclat)[0][-1]
    urcloni = np.where(lon<=urclon)[0][-1]    
    
    glob = subset_raster(glob, llclati, llcloni, urclati, urcloni)
    where_notmask = np.where(True-glob.mask)
    raster_size = np.prod(glob.shape)*4
    if raster_size * n_rasters > memlim:
        warnings.warn('Species %s: Generating this map would require too much memory. Make the bounding box smaller.'%species_name)
    
    for n, ch in zip(short_glob_names, glob_channels):
        rasters[n] = (glob==ch)[where_notmask]    

    for n, p in zip(short_layer_names, layer_names):
        lon,lat,data,t = map_utils.import_raster(*os.path.split(p)[::-1])
        if data.shape != orig_glob_shape:
            raise ValueError, 'Shape of raster %s does not match shape of Glob raster for species %s. Check config file.'%(n,species_name)
        rasters[n] = subset_raster(data, llclati, llcloni, urclati, urcloni)[where_notmask]
        
    ravelledmap = brt_evaluator(rasters)
    out_raster = glob.astype('float32')
    out_raster[where_notmask] = pm.flib.invlogit(ravelledmap)

    return lon[llcloni:urcloni],lat[llclati:urclati],out_raster
