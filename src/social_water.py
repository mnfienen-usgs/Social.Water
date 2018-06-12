from __future__ import print_function
import numpy as np
import email, imaplib
from datetime import datetime, timedelta
import re
import time
import base64
import sys
import os
import csv
import string
import xml.etree.ElementTree as ET
import uuid
# fuzzywuzzy is a fuzzy string matching code from:
# https://github.com/seatgeek/fuzzywuzzy
# note that not really installing it here - just putting the code in locally
import fuzz
import process
import tools
import json


class inpardata:
    def __init__(self,parfilename):
        self.parfilename = parfilename
        
    def read_parfile(self):
    # read in the case-specific parameters from the parfile
        try:
            inpardat = ET.parse(self.parfilename)
        except:
            raise FileOpenFail  
        inpars = inpardat.getroot()
        # obtain EMAIL ACCOUNT INFORMATION
        self.usr = inpars.findall('.//main_account/usr')[0].text
        self.pwd_encoded = inpars.findall('.//main_account/pwd_encoded')[0].text
        self.email_scope = inpars.findall('.//main_account/email_scope')[0].text
        
        # obtain TIMEZONE OFFSETS
        self.dst_time_utc_offset = int(inpars.findall('.//tz_offsets/dst_time_utc_offset')[0].text)
        self.std_time_utc_offset = int(inpars.findall('.//tz_offsets/std_time_utc_offset')[0].text)
        # get the stations and bounds
        self.stations = []
        self.statnums = []
        self.stations_and_bounds = dict()
        stats = inpars.findall('.//stations/station')
        for cstat in stats:
            self.stations_and_bounds[cstat.text]=cstat.attrib
            ##print self.stations_and_bounds[cstat.text]['lbound']
            self.stations_and_bounds[cstat.text]['lbound'] = float(self.stations_and_bounds[cstat.text]['lbound'])
            self.stations_and_bounds[cstat.text]['ubound'] = float(self.stations_and_bounds[cstat.text]['ubound'])
            self.statnums.append(int(re.findall("\d+",cstat.text)[0]))
        self.minstatnum = min(self.statnums)    
        self.maxstatnum = max(self.statnums)
        # get the station_ID keywords
        msg_ids = inpars.findall('.//msg_identifiers/id')
        self.msg_ids = []
        for cv in msg_ids:
            self.msg_ids.append(cv.text)
            
        # get the keywords to remove from messages during parsing
        msg_rms = inpars.findall('.//msg_remove_items/remitem')
        self.msg_rms = []
        for cv in msg_rms:
            self.msg_rms.append(cv.text)


class gage_results:
    # initialize the class
    def __init__(self,gage):
        self.gage = gage
        self.date = list()
        self.datenum = list()
        self.height = list()
        self.users = list()

class timezone_conversion_schedule:
    def __init__(self,start_month,start_day,end_month,end_day):
        self.dst_start_month = start_month
        self.dst_start_day = start_day
        self.dst_end_month = end_month
        self.dst_end_day = end_day

class timezone_conversion_data:
    def __init__(self,site_params):
        # set the timezone-specific values -- currently applies to all measurements
        self.std_time_utc_offset = timedelta(hours = site_params.std_time_utc_offset)
        self.dst_time_utc_offset = timedelta(hours = site_params.dst_time_utc_offset)
        # make a table of DST starting and ending times
        self.dst_start_hour = 2
        self.dst_end_hour = 2
        # these data SUBJECT TO CHANGE --> source is http://www.itronmeters.com/dst_dates.htm
        dst_start_month = [3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 
                3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3]
        
        dst_start_day=[13, 11,  10,  9,  8,  13,  12,  11,  10,  8,  14,  13,  12,  
             10,  9,  8,  14,  12,  11,  10,  9,  14,  13,  12,  11,  9,  8,  14,  13,  11]
        
        dst_end_month=[11, 11,  11,  11,  11,  11,  11,  11,  11,  11,  11, 11,  11,  11,  11,  
              11,  11,  11,  11,  11, 11,  11,  11,  11,  11,  11,  11,  11,  11,  11]
        
        dst_end_day=[6,4,  3,  2,  1,  6,  5,  4,  3,  1,  7,  6,  5,  3,  2, 
                       1,  7,  5,  4,  3,  2,  7,  6,  5,  4,  2,  1,  7,  6,  4]
        
        year=[2011, 2012,  2013,  2014,  2015,  2016,  2017,  2018,  2019,  2020,  
             2021,  2022,  2023,  2024,  2025,  2026,  2027,  2028,  2029,  
             2030,  2031,  2032,  2033,  2034,  2035,  2036,  2037,  2038,  2039,  2040]      
        
        self.dst_sched = dict()
        for i, cyear in enumerate(year):
            self.dst_sched[cyear] = timezone_conversion_schedule(dst_start_month[i],
                                                                 dst_start_day[i],
                                                                 dst_end_month[i],
                                                                 dst_end_day[i])

class email_reader:
    # initialize the class
    def __init__ (self,site_params):
        self.name = 'crowdhydrology'
        self.user = site_params.usr
        self.pwd = (base64.b64decode(site_params.pwd_encoded)).decode()
        self.email_scope = site_params.email_scope
        self.data = dict()
        self.dfmt = '%a, %d %b %Y %H:%M:%S '
        self.outfmt = '%m/%d/%Y %H:%M:%S'
        # make a list of valid station IDs
        self.stations = list(site_params.stations_and_bounds.keys())
        for i in self.stations:
            self.data[i] = gage_results(i)
        self.tzdata = timezone_conversion_data(site_params)
        self.minstatnum = site_params.minstatnum
        self.maxstatnum = site_params.maxstatnum
        self.totals = dict() ## track user contributions
        ## will contain a string user id # followed by a tuple of strings and ints.
        ##( date of first contribution, total contribution count, bad contributions)


    # read the previous data from the CSV files
    def read_CSV_data(self):
    # loop through the stations
        for cg in self.stations:
            if os.path.exists('../data/' + cg.upper() + '.csv'):
                indat = np.genfromtxt('../data/' + cg.upper() + '.csv',dtype=None,delimiter=',',names=True)
                dates = np.atleast_1d(indat['Date_and_Time'])
                gageheight = np.atleast_1d(indat['Gage_Height_ft']) 
                datenum = np.atleast_1d(indat['POSIX_Stamp'])
                try:
                    len_indat = len(indat)
                    for i in range(len_indat):
                        self.data[cg].date.append(dates[i])
                        self.data[cg].height.append(gageheight[i])
                        self.data[cg].datenum.append(datenum[i])
                except:
                        self.data[cg].date.append(dates[0])
                        self.data[cg].height.append(gageheight[0])
                        self.data[cg].datenum.append(datenum[0])
    


    # login in to the server
    def login(self):
        try:
            self.m = imaplib.IMAP4_SSL("imap.gmail.com")
            self.m.login(self.user,self.pwd)
            self.m.select('"[Gmail]/All Mail"')
        except:
            raise LogonFail
        
    # check for new messages
    def checkmail(self):
        # find only new messages
        # other options available 
        # (http://www.example-code.com/csharp/imap-search-critera.asp)
        resp, self.msgids = self.m.search(None, self.email_scope)

    # parse the new messages into new message objects
    def parsemail(self):

        tot_msgs = len(self.msgids[0].split())
        kmess = 0
        self.messages = list()
        for cm in self.msgids[0].split():
            kmess+=1
            kmrat = np.ceil(100*(kmess/float(tot_msgs)))
            if kmess == 0:
                rems = 0
            else:
                rems = np.remainder(100,kmess)
            if rems == 0:
                print('-', end=' ')
                sys.stdout.flush()
            resp, data = self.m.fetch(cm, "(RFC822)")

            msg = email.message_from_string((data[0][1]).decode())

            # print msg['Subject']
            if msg['Subject'] is not None and ('sms from' in msg['Subject'].lower() or 'new text message from' in msg[
                'Subject'].lower()):  # same story here
                # See if the message contains an attachment:
                if msg.get_content_maintype() == 'multipart':
                    # print "Debug: Multipart message found: " + msg['Subject']
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            # This should be the message body, not an attachment
                            self.messages.append(
                                email_message(msg['Date'], msg['Subject'], part.get_payload(decode=True)))  ##
                            break
                else:
                    self.messages.append(email_message(msg['Date'], msg['Subject'], msg.get_payload()))  ##
            print('-', end=' ')
        print ("")

    def extract_gauge_info(self, currmess):
        v = None
        # rip the float out of the line
        try:
            v = tools.find_double(str(currmess.body))
            # print "found val:" + str(v)
        except tools.NoNumError:  # first fail, attempt another!
            self.totals = tools.log_bad_contribution(currmess, self)
        if (v != None):
            currmess.gageheight = v
            userid = currmess.fromUUID
            station = self.stations[currmess.closest_station_match]


            if userid in self.totals:
                contribution_list = self.totals[userid][3]
                if station in contribution_list:
                    contribution_list[station] += 1
                else:
                    contribution_list[station] = 1

                contribution_date_list = self.totals[userid][4]
                if station in contribution_date_list:
                    contribution_date_list[station].append(currmess.datestamp)
                else:
                    contribution_date_list[station] = [currmess.datestamp]

                self.totals[userid] = ( self.totals[userid][0], self.totals[userid][1] + 1, self.totals[userid][2], contribution_list, contribution_date_list)
            else:
                self.totals[userid] = ( currmess.date, 1, 0, {station: 1}, {station: [currmess.datestamp]})

    # now parse the actual messages -- date and body
    def parsemsgs(self,site_params):
        #print("Num of messages: ",len(self.messages))
        # parse through all the messages
        for currmess in self.messages:
            # first the dates
            #print "Debug: currmess.rawdate = " + currmess.rawdate
            tmpdate = re.sub(' \(...\)', '', currmess.rawdate)
            tmpdate = tmpdate[:-5]
            #print "Debug: tmpdate = " + tmpdate
            currmess.date = datetime.strptime(tmpdate,self.dfmt)
            currmess.date = tz_adjust_STD_DST(currmess.date,self.tzdata)
            currmess.dateout = datetime.strftime(currmess.date,self.outfmt)
            currmess.datestamp = time.mktime(datetime.timetuple(currmess.date))

            # now the message bodies
            cm = currmess.body

            if not isinstance(cm,str):
                cm = currmess.body.decode()

            # do a quick check that the message body is only a string - not a list
            # a list happens if there is a forwarded message
            if not isinstance(cm,str):
                cm = cm[0].get_payload()
            maxratio = 0
            maxrat_count = -99999
           # maxrat_line = -99999
            #print '\n\n\nDebug: Original message :' + cm + ':'
            line = cm.lower()
            line = str.rstrip(line,line[str.rfind(line,'sent using sms-to-email'):])
            line = re.sub('(\r)',' ',line)
            line = re.sub('(\n)',' ',line)
            line = re.sub('(--)',' ',line)

            for citem in site_params.msg_ids:
                if citem.lower() in line:
                    currmess.is_gage_msg = True 

            if currmess.robot_status:
                self.process_a_robot_message(currmess)
                continue ##if we have a robot message, we process it and skip the rest.
                ## we don't want these contributions being logged in the same way.

            if currmess.is_gage_msg == True:
                matched = False # set a flag to see if a match has been found
                # now check for the obvious - that the exact station number is in the line
                for j,cs in enumerate(self.stations):
                    # see if there's an exact match first
                    if cs.lower() in line.lower():
                        maxratio = 100
                        maxrat_count = j
                        matched = True
                        # also strip out the station ID, including possibly a '.' on the end
                        line = re.sub(cs.lower()+'\.','',line)
                        line = re.sub(cs.lower(),'',line)                  
                        currmess.station_line = line                        
                        break

                # if no exact match found, get fuzzy!
                if matched == False:
                    # we will test the line, but we need to remove some terms using regex substitutions
                    for cremitem in site_params.msg_rms:
                        line = re.sub('('+cremitem.lower()+')','',line)
                    # now get rid of the floating point values that should be the stage
                    # using regex code from: http://stackoverflow.com/questions/385558/
                    # python-and-regex-question-extract-float-double-value
                    currmess.station_line = line
                    line = re.sub("[+-]? *(?:\d+(?:\.\d*)|\.\d+)(?:[eE][+-]?\d+)?",'', line)
                    ##print line
                    tmp_ints = re.findall("\d+",line)
                    remaining_ints = []
                    for cval in tmp_ints:
                        remaining_ints.append(int(cval))

                    if len(remaining_ints) < 1:
                        maxratio = 0
                        
                    elif ((max(remaining_ints) < self.minstatnum) or 
                        (min(remaining_ints) > self.maxstatnum)):
                        maxratio = 0
                        
                    else:
                        for j,cs in enumerate(self.stations):
                            # get the similarity ratio
                            crat = fuzz.ratio(line,cs)
                            if crat > maxratio:
                                maxratio = crat
                                maxrat_count = j
                currmess.max_prox_ratio = maxratio    
                currmess.closest_station_match = maxrat_count

                self.extract_gauge_info(currmess)


            else:
                ##this message has no readable gauge, so we log it as a bad message.
                ##print "Bad Message" + str(currmess.header)
                tools.log_bad_contribution(currmess, self)
                
    def process_a_robot_message(self, message):
        split_message = str( message.body[0] ).split(",")
        station_id = split_message[1]
        water_height_measured_as = split_message[2]
        temperature_measured_as = split_message[3]
        self.append_robot_data(station_id, water_height_measured_as, temperature_measured_as, message)

    def append_robot_data(self, station, height, temp, message):
        datafile = None
        if not os.path.exists('../data/robot_data/' + station + '.csv'):
            datafile = open( '../data/robot_data/' + station + '.csv', 'w')
            datafile.write("date,measured_water_level,measured_temperature,b64_encoded_original_message\n")
        else:
            datafile = open( '../data/robot_data/' + station + '.csv' , 'a')
        assembled_output_string = ""
        assembled_output_string += str( message.datestamp ) + ","

        assembled_output_string += str( height ) + ","
        assembled_output_string += str( temp ) + ","
        assembled_output_string += str( base64.b64encode( str(message.body[0]) ) )
        assembled_output_string += '\n'
        print("assembled_output_string")
        print(assembled_output_string)
        datafile.write(assembled_output_string)
   
                
    def logout(self):
        self.m.logout()

    def is_duplicate_entry(self, message):
        msg_station = self.stations[message.closest_station_match]

        # Get the data from that message's station (copied over from
        if os.path.exists('../data/' + msg_station.upper() + '.csv'):
            indat = np.genfromtxt('../data/' + msg_station.upper() + '.csv',dtype=None,delimiter=',',names=True, encoding=None)
            datenum = np.atleast_1d(indat['POSIX_Stamp'])
            len_indat = indat.size
            # Loop through every entry
            for i in range(len_indat):
                if datenum[i] == message.datestamp:
                    print("Found a duplicate entry!")
                    return True
        return False

    # for the moment, just re-populate the entire data fields
    def update_data_fields(self,site_params):
        #mnfdebug ofpdebug = open('debug.dat','w')
        for cm in self.messages:
            if not cm.robot_status and cm.is_gage_msg and cm.closest_station_match != -99999:
                lb = site_params.stations_and_bounds[self.stations[cm.closest_station_match]]['lbound']
                ub = site_params.stations_and_bounds[self.stations[cm.closest_station_match]]['ubound']
                if ((cm.gageheight > lb) and  (cm.gageheight < ub)) and not self.is_duplicate_entry(cm):
                    self.data[self.stations[cm.closest_station_match]].date.append(cm.date.strftime(self.outfmt))
                    self.data[self.stations[cm.closest_station_match]].datenum.append(cm.datestamp)
                    self.data[self.stations[cm.closest_station_match]].height.append(cm.gageheight)
                    self.data[self.stations[cm.closest_station_match]].users.append(cm.fromUUID)
                    #print(cm.fromUUID)

                   #mnfdebug ofpdebug.write('%25s%20f%12f%12s\n' %(cm.date.strftime(self.outfmt),cm.datestamp,cm.gageheight,self.stations[cm.closest_station_match]))
        #mnfdebug ofpdebug.close()

    # write all data to CSV files                       
    def write_all_data_to_CSV(self):
        # loop through the stations
        for cg in self.stations:
            datenum = self.data[cg].datenum # posix stamp
            dateval = self.data[cg].date  #date of entry
            gageheight = self.data[cg].height #heights of entries
            userid = self.data[cg].users # list of users
            outdata = np.array(list(zip(datenum,dateval,gageheight,userid)))
            ##print outdata
            if len(outdata) == 0:
                print('%s has no measurements yet' %(cg))
            elif os.path.exists('../data/' + cg.upper() + '.csv'): #If that station has data, just append the new data.
                ofp = open('../data/' + cg.upper() + '.csv','a')
                unique_dates = np.unique(outdata[:,0])
                indies = np.searchsorted(outdata[:,0],unique_dates)
                final_outdata = outdata[indies,:]
                for i in range(len(final_outdata)):
                    
                    ofp.write(final_outdata[i,1] + ',' + str(final_outdata[i,2]) + ',' + str(final_outdata[i,0]) + '\n')
                ofp.close()
            else:
                ofp = open('../data/' + cg.upper() + '.csv','w') 
                ofp.write('Date and Time,Gage Height (ft),POSIX Stamp\n')
                unique_dates = np.unique(outdata[:,0])
                indies = np.searchsorted(outdata[:,0],unique_dates)
                final_outdata = outdata[indies,:]
                for i in range(len(final_outdata)):
                    ofp.write(final_outdata[i,1] + ',' + str(final_outdata[i,2]) + ',' + str(final_outdata[i,0]) + '\n')
                ofp.close()



    def count_contributions(self):
        """Check if there is old user contribution data that needs to be tallied before
           loading up the new data. If there isn't any, we need to rerun all of the messages.
        """
        if os.path.exists("../data"):
                ## read in last time's totals
            if len(sys.argv) > 2 and sys.argv[2] == "-ALL": ##TODO: This might be better if it were moved elsewhere, maybe in the driver?
                self.email_scope = "ALL"

            #TODO: Add in a method for restricting the email search to one specific station?
            # or even better would be a way to just arbitrarily flag settings so you can change
            # whatever you want with the reader for a run, in case something goes wrong and
            # you need to reprocess anything.

          
            if os.path.exists('../data/contributionTotals.csv'):
                totalfile = open('../data/contributionTotals.csv','r')
                totalreader = csv.reader( totalfile, delimiter=',' )
                firstrow = True
                for user in totalreader:
                    if not firstrow:
                        contribution_dict_str = user[4].replace("-", ",").replace("\'", "\"")
                        contribution_date_dict_str = user[5].replace("-", ",").replace("\'", "\"")

                        self.totals[user[0]] = (user[1] , int( user[2] ) , int( user[3] ), json.loads(contribution_dict_str), json.loads(contribution_date_dict_str))
                    firstrow = False
                totalfile.close()
           
                    
    def write_contributions(self):
        """Write hashed user contribution info, useful for tracking total counts and whether there are any users
        who are intentionally giving us bad data.
        """
        totalfile = open('../data/contributionTotals.csv','w')
            #print "writing to " + str( totalfile )
        totalfile.write('contributorID,firstContributionDate,totalContributions,badContributions,validContributionsDict,validContributionsDateDict\n')
        for key in self.totals:
            #print key
            #print(self.totals[key][3])
            totalfile.write( str( key ) + ',' + str( self.totals[key][0] ) + ',' + str( self.totals[key][1] ) + ',' + str( self.totals[key][2] ) + ',' + str( self.totals[key][3] ).replace(",", "-") + ',' + str( self.totals[key][4] ).replace(",", "-") + '\n' )
        totalfile.close()

    def write_station_totals(self):
        """ 
        Write out station data that includes some user data, usefull for tracking 
        general patterns of use among users. Do we have heavily localized use? Do we have 
        people who use consistenly for long periods of time, or do people conribute sparsely?
        Do power users burn out and get bored? These sorts of things.
        """

        for cg in self.stations:
            result = self.data[cg]
            output = open( "../data/"+ str(result.gage) + "_StationTotals.csv", 'w' )
            all_results = list(zip( result.users, result.date, result.height, [result.gage]*len(result.users) ))
            for item in all_results:
                output.write(str(item[0]) + ','  + str(item[1]) + ',' + str(item[2]) + ',' + str(item[3]) + '\n')
            output.close()

    # plot the results in a simple time series using Dygraphs javascript (no Flash ) option
    def plot_results_dygraphs(self):
        # loop through the stations
        for cg in self.stations:
            hh = '../charts/' + cg.upper() + '_dygraph.html'
            if os.path.exists('../charts/' + cg.upper() + '_dygraph.html') == 0:
                header = ('<!DOCTYPE html>\n<html>\n' +
                        '  <head>\n' +
                        '    <meta http-equiv="X-UA-Compatible" content="IE=EmulateIE7; IE=EmulateIE9">\n' +
                        '    <!--[if IE]><script src="js/graph/excanvas.js"></script><![endif]-->\n' +
                        '  </head>\n' +
                            '  <body>\n' +
                            "  "*2 + '<script src="js/graph/dygraph-combined.js" type="text/javascript"></script> \n'+
                            "  "*3 +  '<div id="graphdiv"></div>\n<script>\n')
                footer = ("  "*4 + 'g = new Dygraph(\n' +
                        "  "*4 + 'document.getElementById("graphdiv"),\n' +
                        "  "*4 + '"../data/%s.csv",\n' %cg + 
                        "  "*4 + '{   title: "Hydrograph at ' +cg+ '",\n'  + 
                        "  "*4 + "labelsDivStyles: { 'textAlign': 'right' },\n" +
                        "  "*4 + 'showRoller: true,\n' + 
                        "  "*4 + "xValueFormatter: Dygraph.dateString_,\n" + 
                        "  "*4 + "xTicker: Dygraph.dateTicker,\n" +
                        "  "*4 + "labelsSeparateLines: true,\n" +
                        "  "*4 + "labelsKMB: true,\n" +
                        "  "*4 + "visibility: [true,false],\n" +                    
                        "  "*4 + "drawXGrid: false,\n" + 
                        "  "*4 + " width: 640,\n" + 
                        "  "*4 + "height: 300,\n" +
                        "  "*4 + "xlabel: 'Date',\n" + 
                        "  "*4 + "ylabel: 'Gage Height (ft.)',\n" + 
                        "  "*4 + 'colors: ["blue"],\n' + 
                        "  "*4 + "strokeWidth: 2,\n" + 
                        "  "*4 + "showRangeSelector: true\n"  +
                        "  "*4 + "}\n" +
                        "  "*4 + ");\n" +
                        "</script>\n</body>\n</html>\n")

            
                self.data[cg].charttext = header  + footer
                ofp = open('../charts/' + cg.upper() + '_dygraph.html','w')
                ofp.write(self.data[cg].charttext)
                ofp.close()

def tz_adjust_STD_DST(cdateUTC,tzdata):
    # make the adjustment, based on 2012 and onward, STD/DST schedule
    # based on the general schedule and data provided by the user
    cyear= cdateUTC.year
    
    dst_start = datetime(cyear,tzdata.dst_sched[cyear].dst_start_month,
                         tzdata.dst_sched[cyear].dst_start_day,
                         tzdata.dst_start_hour)
    dst_end = datetime(cyear,tzdata.dst_sched[cyear].dst_end_month,
                       tzdata.dst_sched[cyear].dst_end_day,
                       tzdata.dst_end_hour)
    # see if the current time in UTC falls within DST or not and adjust accordingly
    if ((cdateUTC >= dst_start) and (cdateUTC <= dst_end)):
        cdate = cdateUTC - tzdata.dst_time_utc_offset
    else:
        cdate = cdateUTC - tzdata.std_time_utc_offset
    return cdate


    
class email_message:
    # initialize an individual message
    def __init__(self,date,header,txt):

        self.is_gage_msg = False
        self.header=header
        self.body=txt
        self.rawdate = date
        self.date = ''
        self.dateout = ''
        self.max_prox_ratio = 0
        self.closest_station_match = ''
        self.station_line = ''
        self.gageheight = -99999
       
        number = str( self.header ).lower()
        number = tools.remove_chars(number, "()- smfrom")
        ##print "HASHING1: " + number,
        hasher = uuid.uuid3( uuid.NAMESPACE_OID, number ) 
        self.fromUUID=  ( str(hasher) )
        self.robot_status = self.check_self_for_robot_status()

    def check_self_for_robot_status(self):
        ROBOT_STATUS = "IMAROBOT"
        #checks for the "IMAROBOT" magic number and returns the status
        #print(self.body)


        return self.body is not None and ROBOT_STATUS in str( self.body )




        
           
# ####################### #
# Error Exception Classes #        
# ####################### #
# -- cannot log on
class LogonFail(Exception):
    def __init__(self,username):
        self.name=username
    def __str__(self):
        return('\n\nLogin Failed: \n' +
               'Cannot log on ' + self.name)

# -- user did not provide a parameter filename when calling sw_driver.py
class NoParfileFail(Exception):
    def __init__(self):
        self.err = ''
    def __str__(self):
        return('\n\nCould not find parameter filename. \n' +
               'Call should be made as "python sw_driver.py <parfilename>"\n' +
               'where <parfilename> is the name of a parameter file.')
# -- cannot open an input file
class FileOpenFail(Exception):
    def __init__(self,filename):
        self.fn = filename
    def __str__(self):
        return('\n\nCould not open %s.' %(self.fn))    
# -- Invalid station value for bounds
class InvalidBounds(Exception):
    def __init__(self,statID):
        self.station = statID
    def __str__(self):
        return('\n\nStation "%s" not in the list of stations above.\nCheck for consistency.' %(self.station))





