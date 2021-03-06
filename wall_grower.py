from pointcloud import Pointcloud
from data_handler import DataLoader
import graphs
import itertools 
import numpy as np
from scipy import odr
from copy import deepcopy

def odr_linear_definition(B, x):
    #FIXME: is a line represented this way capable of 
    #fitting vertical lines far from the origin? 
    #I think we can put whatever formulation we need here,
    #but I don't really understand what it's doing
    return B[0]*x + B[1]

class ODR_Fit(object):
    """fit scipy odr, and provide methods for bounding the resulting line"""
    def __init__(self, list_of_points, pointcloud):
        self.points = list_of_points 
        model = odr.Model(odr_linear_definition)

        #extract coordinates from list of points
        x = [pointcloud.points[p][0] for p in list_of_points]
        y = [pointcloud.points[p][1] for p in list_of_points]

        #calculate standard errors along dimensions or something
        wd = 1/(np.std(x)**2) #FIXME: I have no idea what this is 
        we = 1/(np.std(y)**2) #or if I did it right

        #fit ODR
        data = odr.Data(x, y, wd=wd, we=we)
        self.odr = odr.ODR(data, model, beta0=[1., 1.])
        self.odr.run()

        params = self.odr.output.beta 

        #apply bounds for start and end point
        bounds = []
        for index in [-1, 0]:
            m, b = params 
            p_x, p_y = pointcloud.points[self.points[index]]
            bounds.append(self._find_closest_input_val(m, b, p_x, p_y))
        bounds.sort()

        #save the params in nice formats
        params = {"m": params[0], "b": params[1], "low": bounds[0], "high": bounds[1]}
        self.params = params
        self.start_point = [params["low"], params["low"] * params["m"] + params["b"]]
        self.end_point = [params["high"], params["high"] * params["m"] + params["b"]]



    def _find_closest_input_val(self, m, b, p_x, p_y):
        #found by taking the derivative of the distance 
        #function wrt x then solving for x
        return (-b*m+m*p_y+p_x)/(m**2+1)



class WallGrower(object):
    def __init__(self, pointcloud):
        self.pointcloud = pointcloud

    def make_network(self, max_distance, corner_threshold=2.5, min_length=4):
        """replace the pointcloud with a collection of lines. 
        The lines are generated like so: 

        for every point:
            find its two nearest neighbors 
            if the distance to the closest neighbor is less than max_distance
                draw a line between the two points 
                if the distance to the second neighbor is within the max_distance 
                and is less than first_distance * max_second_ratio
                    draw a line between the point and the second neighbor """
        
        #we will store each points two closest neighbors, if they exist
        linesegments = {}

        #extract line segments
        #FIXME: the kdtree query method used in the get_nearest_neighbors call 
        #has a max distance term that speeds up computation. I'm ignoring it 
        #FIXME: after extracting lines made up of close points, it may be good 
        # to run another line extraction phase using larger distance tolerances
        # on the remaining points
        all_distances, all_indexes = self.pointcloud.get_nearest_neighbors(3)
        for i in range(len(all_distances)):
            linesegments[i] = set()
            distances = all_distances[i]
            indexes = all_indexes[i]
            #each location we are querying is a point 
            #in the pointcloud, so the closest neighbor 
            #is always itself. So we start at 1:
            if distances[1] < max_distance:
                linesegments[i].add(indexes[1])
                if distances[2] < max_distance:
                    linesegments[i].add(indexes[2])

        #lines is a collection of vectors between neighboring points
        #these vectors make connected subgroups we want to be able to 
        #look at individually
        subgroups = self._extract_subgroups(deepcopy(linesegments))

        #subgroups are typically mostly linear, because of the nature 
        #of the data and the preprocessing we did, but they aren't perfect. 
        #This extracts the longest polyline it's possible to make from a subgroup
        polylines = [self._arrange_into_line(sg) for sg in subgroups]

        #split up polylines at the corners
        polylines = self._split_up_polylines(polylines, corner_threshold=corner_threshold)

        #remove polylines that are empty 
        #FIXME: there might be a bug causing small subgroups to 
        #fail to form lines, but that's kind of a feature 
        polylines = [pl for pl in polylines if len(pl)]

        #replace each polyline with a single line, bounded by the 
        #endpoints with parameters determined by ODR fitting
        lines = [self._fit_bounded_odr(pl) for pl in polylines]

        #remove small lines 
        lines = [l for l in lines if len(l.points) >= min_length]

        #return the detected walls
        return linesegments, lines

    def _check_similarity(self, a_line, b_line, euc_tolerance=.1, polar_tolerance=.1):
        """determine if two lines can be combined"""
        #first check if slopes lie within boundary 
        theta0 = np.arctan(a_line.params["m"])
        theta1 = np.arctan(b_line.params["m"])
        diff = np.abs(theta0 - theta1)
        if diff > polar_tolerance:
            return False

        #check that the start and end points are close enough
        for order in [[a_line, b_line], [b_line, a_line]]:
            if self._euclidean_distance(order[0].end_point[0], order[0].end_point[1], order[0].start_point[0], order[0].start_point[1]) < euc_tolerance:
                return True
        return False

    def _fit_bounded_odr(self, list_of_points):
        """first, use every point to fit
        an orthogonal distance line. 
        Then, find the points on the odr_line that are closest
        to the start and endpoints of the list_of_points. """
        odr =  ODR_Fit(list_of_points, self.pointcloud)
        return odr

    def _arrange_into_line(self, linesegments):
        """produce the longest polyline possible from 
        the passed connected group of linesegments."""
        #we want to take an ordered subset of the 
        #linesegments to make the longest possible 
        #line. 
        #the end of the line are points with only one 
        #connection
        endpoints = self._find_endpoints(linesegments)

        #now we try growing a line from each endpoint
        longest_found = []
        longest_length = 0
        for endpoint in endpoints:
            order = self._order_segments_from(endpoint, linesegments)
            #FIXME: if the last point of this order is an endpoint we 
            # haven't tested yet, we can remove it
            if len(order) > longest_length:
                longest_length = len(order)
                longest_found = order 

        return longest_found

    def _find_endpoints(self, linesegments):
        """Find all points in the connected group 
        of linesegments that have only one attached line."""
        #linesegments must be symmetric for this to work
        for key in linesegments:
            for otherkey in linesegments[key]:
                linesegments[otherkey].add(key)

        endpoints = []
        for index in linesegments:
            if len(linesegments[index]) == 1:
                endpoints.append(index)
        return endpoints

    def _order_segments_from(self, current_point, linesegments, already_included=None):
        """start at self.lines[key]. If there are neighboring points that 
        aren't in the already_included set, call _follow_connections on each of 
        them and return the longest sequence generated. If there are no neighboring
        points, return the current point. """ 
        if already_included is None:
            already_included = set()

        #remove points already in this line from the possible next points
        options_from_here = set(linesegments[current_point]) - already_included
 
        if not len(options_from_here):
            return [current_point]

        #FIXME: we don't need to recurse if there's only one option
        longest_found = []
        longest_length = 0
        for option in options_from_here:
            already_included.add(option)
            this_option_path = self._order_segments_from(option, linesegments, already_included)
            already_included.remove(option)
            if len(this_option_path) > longest_length:
                longest_length = len(this_option_path)
                longest_found = this_option_path
        return [current_point] + longest_found
        
    def _extract_subgroups(self, lines):
        """split up the passed linesegments into connected 
        groups of linesegments"""
         #each point can have more than one 
         #line pointing at it. First, we need 
         #to make lines symmetric 
        for key in lines:
            for otherkey in lines[key]:
                lines[otherkey].add(key)
        self.lines = lines 
        polylines = []
        while self.lines:
            polylines.append(self._extract_subgroup(next(iter(self.lines))))
        return polylines

    def _extract_subgroup(self, key):
        """pull out all linesegments that have a path to key"""
        polyline = self._follow_connections(key)
        #delete all nodes in network from self.lines
        for point_index in list(polyline.keys()):
            del self.lines[point_index]
        return polyline

    def _follow_connections(self, key, linesegments=None, already_included=None):
        """recursively gather all the neighbors of the point 
        indexed by key and add the line segments to group, unless the 
        next point is in already_included"""

        #is this the first start case or a recursive call 
        if linesegments is None:
            linesegments = {}
            already_included = set()

        #grab the connections for this point 
        connections = self.lines[key]

        #create an entry for this point in the polylines object 
        linesegments[key] = set()

        #loop over this points neighbors
        for connection in connections:
            #check the other point hasn't already been included 
            if not connection in already_included:
                #add the line to this point to the polyline 
                linesegments[key].add(connection)
                already_included.add(connection)
                #follow this points connections
                self._follow_connections(connection, linesegments, already_included)
        return linesegments

    def _split_up_polylines(self, polylines, corner_threshold=2.5):
        """each polyline is an ordered array of indexes to 
        the self.points object. 

        Each point in self.points has an x and y position. Therefore,
        every point except the first and last has an 
        angle. We want to break up the polylines in places where 
        the angle (always represented in radians across the acute side)
        is less than corner_threshold.

        A long gradual curve will not be split up by this technique. """
        new_polylines = []
        for pl in polylines:
            new_polylines += self._split_up_polyline(pl, corner_threshold)
        return new_polylines

    def _split_up_polyline(self, pl, corner_threshold):
        completed_lines = []
        current_line = []
        for i in range(1, len(pl) - 2):
            #calculate angle between the points 
            d12 = self._distance_between_points(pl[i - 1], pl[i])
            d13 = self._distance_between_points(pl[i - 1], pl[i + 1])
            d23 = self._distance_between_points(pl[i], pl[i + 1])
            angle = np.arccos((d12**2 + d23**2 - d13**2)/(2 * d12 * d23))
            #https://stackoverflow.com/questions/1211212/how-to-calculate-an-angle-from-three-points

            #add the current point to the current line
            current_line.append(pl[i])
            #if this point is a corner, start a new line
            if angle < corner_threshold:
                completed_lines.append(current_line)
                current_line = [pl[i]]
        completed_lines.append(current_line)
        return completed_lines

    def _distance_between_points(self, index_one, index_two):
        return self._euclidean_distance(
            self.pointcloud.points[index_one][0], 
            self.pointcloud.points[index_one][1],
            self.pointcloud.points[index_two][0], 
            self.pointcloud.points[index_two][1])

    def _euclidean_distance(self, x0, y0, x1, y1):
        return np.sqrt((x0-x1)**2 + (y0-y1)**2)


data_loader = DataLoader("data_2020-06-10-10-24-18.bag")

while True:
    pointcloud = Pointcloud(data_loader.load_next_frame())
    
    pointcloud.remove_floor(floor=.05)
    graphs.graph_pointcloud(pointcloud, .5, c="yellow", title="points above floor")

    pointcloud.take_xy()

    pointcloud.take_percentage(.5)
    graphs.graph_pointcloud(pointcloud, s=10, c="yellow", title="randomly undersampled")

    pointcloud.biased_undersample(percentile=.1, radius=.6)
    graphs.graph_pointcloud(pointcloud, s=20, c="orange", title="biased undersampled")

    pointcloud.take_centroids(400, exact=True)
    graphs.graph_pointcloud(pointcloud, s=40, c="red", title="kmeans centroid replaced")
    
    wg = WallGrower(pointcloud)
    linesegments, lines = wg.make_network(max_distance=.4, corner_threshold=2.7, min_length=3)
    graphs.graph_line_segments(linesegments, pointcloud, colors=["gray"])
    graphs.graph_slope_intercept_lines(lines, pointcloud, colors=["black"], l=3)
    graphs.show_graphs(title="Located Walls of 10/22 First LIDAR Scan")

